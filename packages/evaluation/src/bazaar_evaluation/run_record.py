"""Local mirror of the runner's RunRecord v1, adapted into evaluator inputs.

Evals never imports the runner. Unknown fields are ignored so runner additions don't break us;
the nested protocol models stay strict. Moves to bazaar_protocol after the demo.
"""

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal
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
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from bazaar_evaluation.config import DEMO_CONFIG, EvaluatorConfig
from bazaar_evaluation.emit import emit
from bazaar_evaluation.evaluate import evaluate_run
from bazaar_evaluation.inputs import RunEvidence, RunOutcome
from bazaar_evaluation.results import RunEvaluation, TraceId

Sequence = Annotated[int, Field(ge=0, strict=True)]


class _Mirror(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Manifest(_Mirror):
    experiment_id: UUID
    agent_id: UUID
    account_id: UUID
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
    final_account: AccountSnapshot


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
    failure = record.failure
    if record.status == "failed" and not (failure or "").strip():
        failure = "run failed; the record gave no failure reason"  # v1 allows a null failure
    outcome = RunOutcome(
        marks=tuple(k.snapshot for k in sorted(record.marks, key=lambda k: k.event_sequence)),
        final_account=record.final_account,
        run_status=record.status,
        run_failure=failure,
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
    evaluation = evaluate_run(*to_inputs(run), config)
    m = run.manifest
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
