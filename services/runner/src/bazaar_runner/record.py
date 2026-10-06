"""RunRecord v1: the one JSON file per run that evals and replay read, and the traced run around it.

Evals mirrors this shape (bazaar_evaluation.run_record) and never imports the runner.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

import logfire
from bazaar_protocol import AccountSnapshot, NonNegativeAmount, Version, WireModel
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from bazaar_runner.clock import build_schedule, schedule_digest
from bazaar_runner.market import MarketPort
from bazaar_runner.policy import DecisionPolicy
from bazaar_runner.run import (
    FailureCode,
    MarkRecord,
    OrderRecord,
    RunResult,
    RunSpec,
    RunState,
    run_strategy,
)

logger = logging.getLogger(__name__)

TraceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Evaluate = Callable[[dict], BaseModel]


class RecordManifest(WireModel):
    experiment_id: UUID
    agent_id: UUID
    # None only for a run refused before its account existed (for example approval_denied).
    account_id: UUID | None
    strategy_version_id: UUID
    approval_id: UUID
    data_version: Version
    execution_rule_version: Version
    schedule_digest: Annotated[str, Field(min_length=1)]
    period_start: AwareDatetime
    period_end: AwareDatetime
    starting_cash: NonNegativeAmount
    policy_ref: Annotated[str, Field(min_length=1)]


class RunRecord(WireModel):
    record_version: Literal["runrecord-v1"] = "runrecord-v1"
    manifest: RecordManifest
    status: Literal["completed", "failed"]
    failure: str | None = None
    # A runner addition; evals ignores unknown fields.
    failure_code: FailureCode | None = None
    trace_id: TraceId | None = None
    orders: tuple[OrderRecord, ...] = ()
    marks: tuple[MarkRecord, ...] = ()
    final_account: AccountSnapshot | None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if self.status == "failed":
            if not self.failure:
                raise ValueError("a failed run must say why")
            return self
        if self.final_account is None or self.manifest.account_id is None:
            raise ValueError("a completed run has an account")
        # Evals scores the whole period: the run must end on the closing mark, after every order.
        last = self.marks[-1] if self.marks else None
        if last is None or last.snapshot.simulated_at != self.manifest.period_end:
            raise ValueError("a completed run ends with a mark at period_end")
        if self.orders and self.orders[-1].event_sequence >= last.event_sequence:
            raise ValueError("the closing mark must come after the last order")
        return self


def build_record(
    spec: RunSpec, result: RunResult, policy_ref: str, trace_id: str | None
) -> RunRecord:
    sessions = spec.script.sessions
    account = result.account
    return RunRecord(
        manifest=RecordManifest(
            **spec.model_dump(
                include={
                    "experiment_id",
                    "agent_id",
                    "strategy_version_id",
                    "approval_id",
                    "data_version",
                    "execution_rule_version",
                }
            ),
            account_id=account.account_id if account else None,
            schedule_digest=schedule_digest(build_schedule(spec.script)),
            period_start=sessions[0].open_at,
            period_end=sessions[-1].close_at,
            starting_cash=spec.starting_cash,
            policy_ref=policy_ref,
        ),
        status="completed" if result.state is RunState.COMPLETED else "failed",
        failure=result.failure,
        failure_code=result.failure_code,
        trace_id=trace_id,
        orders=result.orders,
        marks=result.marks,
        final_account=account,
    )


def _trace_id(span: logfire.LogfireSpan) -> str | None:
    # None when Logfire is not configured: the span is a no-op without a context.
    context = span.get_span_context()
    return format(context.trace_id, "032x") if context is not None and context.is_valid else None


async def record_run(
    spec: RunSpec,
    market: MarketPort,
    policy: DecisionPolicy,
    *,
    policy_ref: str,
    runs_dir: Path,
    evaluate: Evaluate | None = None,
) -> tuple[RunRecord, BaseModel | None]:
    """Run, build the record and evaluate it inside one runner.run span, then write the files."""
    run_dir = runs_dir / str(spec.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    with logfire.span(
        "runner.run", experiment_id=str(spec.experiment_id), policy_ref=policy_ref
    ) as span:
        result = await run_strategy(spec, market, policy)
        record = build_record(spec, result, policy_ref, _trace_id(span))
        span.set_attribute("status", record.status)
        if record.status == "failed":
            span.set_attribute("failure_code", record.failure_code)
            span.set_attribute("failure", record.failure)
            span.set_level("error")
        # Written before evaluation, so an evaluator failure can never lose the run.
        (run_dir / "record.json").write_text(record.model_dump_json(indent=2))
        evaluation = _evaluate(evaluate, record) if evaluate else None

    if evaluation is not None:
        (run_dir / "evaluation.json").write_text(evaluation.model_dump_json(indent=2))
    return record, evaluation


def _evaluate(evaluate: Evaluate, record: RunRecord) -> BaseModel | None:
    # Inside runner.run, so evals' own spans join the run's trace.
    try:
        with logfire.span("runner.evaluate"):
            return evaluate(record.model_dump(mode="json"))
    except Exception:
        logger.exception("evaluation failed for experiment %s", record.manifest.experiment_id)
        return None
