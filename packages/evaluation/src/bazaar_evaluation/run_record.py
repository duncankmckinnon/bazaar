"""Local mirror of the runner's RunRecord v1, adapted into evaluator inputs.

Evals never imports the runner. Unknown fields are ignored so runner additions don't break us;
the nested protocol models stay strict. Moves to bazaar_protocol after the demo.
"""

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from bazaar_protocol import (
    AccountSnapshot,
    ExperimentContext,
    NonNegativeAmount,
    OrderRequest,
    OrderResult,
    PortfolioSnapshot,
    Version,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from bazaar_evaluation.config import DEMO_CONFIG, EvaluatorConfig
from bazaar_evaluation.emit import emit
from bazaar_evaluation.evaluate import evaluate_run, period_denominators
from bazaar_evaluation.inputs import RunEvidence, RunOutcome
from bazaar_evaluation.results import (
    Evidence,
    PeriodSummary,
    RunEvaluation,
    ScoreStatus,
    TraceId,
)

Sequence = Annotated[int, Field(ge=0, strict=True)]


class _Mirror(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Manifest(_Mirror):
    experiment_id: UUID
    agent_id: UUID
    account_id: UUID | None  # null when a launch was refused before an account existed
    strategy_version_id: UUID
    approval_id: UUID
    data_version: Version
    execution_rule_version: Version
    schedule_digest: Annotated[str, Field(min_length=1)]
    period_start: AwareDatetime
    period_end: AwareDatetime
    starting_cash: NonNegativeAmount
    policy_ref: Annotated[str, Field(min_length=1)]


class RecordedOrder(_Mirror):
    event_sequence: Sequence
    order_index: Sequence
    decided_at: AwareDatetime
    request: OrderRequest
    result: OrderResult


class RecordedMark(_Mirror):
    event_sequence: Sequence
    snapshot: PortfolioSnapshot


class RunRecord(_Mirror):
    record_version: Literal["runrecord-v1"]
    manifest: Manifest
    status: Literal["completed", "failed"]
    failure: str | None = None
    trace_id: TraceId | None = None
    orders: tuple[RecordedOrder, ...] = ()
    marks: tuple[RecordedMark, ...] = ()
    final_account: AccountSnapshot | None

    @model_validator(mode="after")
    def account_present_unless_failed(self) -> Self:
        no_account = self.manifest.account_id is None
        if self.status == "completed" and (no_account or self.final_account is None):
            raise ValueError("a completed record needs manifest.account_id and final_account")
        if no_account and (self.orders or self.marks or self.final_account):
            raise ValueError("orders, marks and final_account need an account")
        return self

    @property
    def failure_reason(self) -> str | None:
        if self.status == "failed" and not (self.failure or "").strip():
            return "run failed; the record gave no failure reason"  # v1 allows a null failure
        return self.failure


def to_inputs(record: RunRecord) -> tuple[RunEvidence, RunOutcome]:
    m = record.manifest
    ids = {
        "experiment_id": m.experiment_id,
        "agent_id": m.agent_id,
        "account_id": m.account_id,
        "strategy_version_id": m.strategy_version_id,
    }
    evidence = RunEvidence(
        context=ExperimentContext(
            **ids,
            approval_id=m.approval_id,
            simulated_at=m.period_start,
            event_sequence=0,
            data_version=m.data_version,
            execution_rule_version=m.execution_rule_version,
        ),
        opening_account=AccountSnapshot(
            **ids, simulated_at=m.period_start, state_version=0, cash=m.starting_cash
        ),
        orders=tuple(
            o.result for o in sorted(record.orders, key=lambda o: (o.event_sequence, o.order_index))
        ),
    )
    outcome = RunOutcome(
        marks=tuple(k.snapshot for k in sorted(record.marks, key=lambda k: k.event_sequence)),
        final_account=record.final_account,
        run_status=record.status,
        run_failure=record.failure_reason,
    )
    return evidence, outcome


def evaluate_and_emit(
    record: Mapping[str, Any] | str | bytes, config: EvaluatorConfig = DEMO_CONFIG
) -> RunEvaluation:
    """The runner's entry point. Raises only on a malformed record; failed or unsupported
    evaluations are results."""
    if isinstance(record, (str, bytes)):
        record = json.loads(record)
    run = RunRecord.model_validate(record)
    m = run.manifest
    if m.account_id is None:
        evaluation = _refused_launch(run, config)
    else:
        evaluation = evaluate_run(*to_inputs(run), config)
    # Revalidate rather than model_copy, so pass-through labels meet RunEvaluation's rules (UTC).
    evaluation = RunEvaluation.model_validate(
        evaluation.model_dump()
        | {
            "period_start": m.period_start,
            "period_end": m.period_end,
            "starting_cash": m.starting_cash,
            "policy_ref": m.policy_ref,
            "trace_id": run.trace_id,
        }
    )
    emit(evaluation)
    return evaluation


def _refused_launch(record: RunRecord, config: EvaluatorConfig) -> RunEvaluation:
    """No account was ever opened, so there is nothing to score and no account is invented."""
    m = record.manifest
    period = PeriodSummary(
        account_id=None,
        experiment_id=m.experiment_id,
        status=ScoreStatus.UNSUPPORTED,
        evaluator_version=config.evaluator_version,
        evidence=(
            Evidence(data_version=m.data_version, execution_rule_version=m.execution_rule_version),
            Evidence(reason=f"launch refused before an account existed: {record.failure_reason}"),
        ),
        denominators=period_denominators((), [], 0),
    )
    return RunEvaluation(
        experiment_id=m.experiment_id,
        account_id=None,
        agent_id=m.agent_id,
        strategy_version_id=m.strategy_version_id,
        approval_id=m.approval_id,
        data_version=m.data_version,
        execution_rule_version=m.execution_rule_version,
        evaluator_version=config.evaluator_version,
        run_status=record.status,
        run_failure=record.failure_reason,
        trade_scores=(),
        period=period,
    )
