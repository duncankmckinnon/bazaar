"""Drive one approved strategy run over the scripted clock against a market port."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    OrderResult,
    PortfolioSnapshot,
    PositiveAmount,
    Version,
    WireModel,
)
from pydantic import Field

from bazaar_runner.clock import ClockScript, EventKind, RunManifest, build_schedule
from bazaar_runner.market import ApprovalDenied, MarketError, MarketPort
from bazaar_runner.policy import DecisionPolicy

Index = Annotated[int, Field(ge=0, strict=True)]
# The market's error code, or one of two runner codes. T4, evals and Logfire match on it.
FailureCode = Literal["approval_denied", "policy_error"] | ErrorCode


class RunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunSpec(WireModel):
    run_id: UUID
    experiment_id: UUID
    agent_id: UUID
    strategy_version_id: UUID
    approval_id: UUID
    data_version: Version
    execution_rule_version: Version
    starting_cash: PositiveAmount
    script: ClockScript


class OrderRecord(WireModel):
    event_sequence: Index
    # Position within its decision; orders fill at the decision's simulated_at (A4).
    order_index: Index
    result: OrderResult


class MarkRecord(WireModel):
    event_sequence: Index
    portfolio: PortfolioSnapshot


class RunResult(WireModel):
    run_id: UUID
    state: RunState
    # None only when the account was never created.
    account: AccountSnapshot | None
    orders: tuple[OrderRecord, ...]
    marks: tuple[MarkRecord, ...]
    failure: str | None = None
    failure_code: FailureCode | None = None


def failure_code(exc: Exception) -> FailureCode:
    if isinstance(exc, ApprovalDenied):
        return "approval_denied"
    if isinstance(exc, MarketError):
        return exc.detail.code
    return "policy_error"


async def run_strategy(spec: RunSpec, market: MarketPort, policy: DecisionPolicy) -> RunResult:
    """Run every scheduled event once. A failure stops the run but keeps what already settled."""
    schedule = build_schedule(spec.script)
    account: AccountSnapshot | None = None
    orders: list[OrderRecord] = []
    marks: list[MarkRecord] = []
    state, failure, code = RunState.RUNNING, None, None

    async def set_cutoff(cutoff: datetime) -> None:
        await market.set_cutoff(
            spec.experiment_id, cutoff, spec.data_version, spec.execution_rule_version
        )

    try:
        await set_cutoff(spec.script.sessions[0].open_at)
        account = await market.create_account(
            spec.experiment_id,
            spec.agent_id,
            spec.strategy_version_id,
            spec.starting_cash,
            request_id=spec.run_id,
        )
        manifest = RunManifest(
            **spec.model_dump(include=set(RunManifest.model_fields)),
            account_id=account.account_id,
        )
        for event in schedule:
            ctx = event.context(manifest)
            await set_cutoff(event.simulated_at)
            if event.kind is EventKind.MARK:
                portfolio = await market.portfolio(ctx)
                marks.append(MarkRecord(event_sequence=event.event_sequence, portfolio=portfolio))
                continue
            account = await market.account(ctx)
            decision = await policy(ctx, account)
            for index, order in enumerate(decision):
                result = await market.submit(ctx, order)
                orders.append(
                    OrderRecord(
                        event_sequence=event.event_sequence, order_index=index, result=result
                    )
                )
                account = result.account
        state = RunState.COMPLETED
    except Exception as exc:  # noqa: BLE001 - any policy or market failure ends the run as failed
        state, failure, code = RunState.FAILED, f"{type(exc).__name__}: {exc}", failure_code(exc)

    if account is not None:
        try:
            account = await market.close_account(spec.experiment_id, account.account_id)
        except Exception as exc:  # noqa: BLE001 - keep the run's record even if closing fails
            state, code = RunState.FAILED, code or failure_code(exc)
            failure = f"{failure}; " if failure else ""
            failure += f"close_account failed, account left open: {type(exc).__name__}: {exc}"

    return RunResult(
        run_id=spec.run_id,
        state=state,
        account=account,
        orders=tuple(orders),
        marks=tuple(marks),
        failure=failure,
        failure_code=code,
    )
