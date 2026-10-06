"""Drive one approved strategy run over the scripted clock against a market port."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

import logfire
from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ExperimentContext,
    OrderRequest,
    OrderResult,
    PortfolioSnapshot,
    PositiveAmount,
    RejectedOrder,
    Version,
    WireModel,
)
from pydantic import AwareDatetime, Field

from bazaar_runner.clock import ClockScript, EventKind, RunManifest, build_schedule, utc_z
from bazaar_runner.market import (
    ApprovalDenied,
    FutureData,
    MarketError,
    MarketPort,
    RunnerUnauthorized,
)
from bazaar_runner.policy import DecisionPolicy

Index = Annotated[int, Field(ge=0, strict=True)]
# The market's error code, or a runner code. T4, evals and Logfire match on it.
FailureCode = (
    Literal["approval_denied", "future_data", "runner_unauthorized", "policy_error"] | ErrorCode
)


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
    decided_at: AwareDatetime
    request: OrderRequest
    result: OrderResult


class MarkRecord(WireModel):
    event_sequence: Index
    snapshot: PortfolioSnapshot


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
    if isinstance(exc, FutureData):
        return "future_data"
    if isinstance(exc, RunnerUnauthorized):
        return "runner_unauthorized"
    if isinstance(exc, MarketError):
        return exc.detail.code
    return "policy_error"


def describe_failure(
    exc: Exception, spec: "RunSpec", step: str, account: AccountSnapshot | None
) -> str:
    """One readable sentence for Logfire and the leaderboard: no exception repr, no secret."""
    if isinstance(exc, ApprovalDenied):
        sentence = (
            f"approval denied: approval {spec.approval_id} is not approved"
            f" for experiment {spec.experiment_id}"
        )
        if account is None:
            sentence += "; refused before any account was opened"
        return sentence
    if isinstance(exc, RunnerUnauthorized):
        return f"runner unauthorized {step}: the market refused the runner's credential"
    if isinstance(exc, FutureData):
        return f"future data refused {step}: a read asked for data past the experiment's clock"
    if isinstance(exc, MarketError):
        # The adapter has already redacted the runner token from market messages.
        return f"market error {step} ({exc.detail.code}): {exc.detail.message}"
    return f"policy error {step}: the run stopped on {type(exc).__name__}"


def _at(kind: str, event_sequence: int, simulated_at: datetime) -> str:
    return f"during the {kind} at {utc_z(simulated_at)} (event {event_sequence})"


async def _submit(
    market: MarketPort,
    ctx: ExperimentContext,
    index: int,
    order: OrderRequest,
    orders: list[OrderRecord],
) -> AccountSnapshot:
    with logfire.span(
        "runner.order", symbol=order.symbol, side=order.side.value, quantity=str(order.quantity)
    ) as span:
        result = await market.submit(ctx, order)
        span.set_attribute("status", result.status)
        if isinstance(result, RejectedOrder):
            span.set_attribute("error_code", result.error.code.value)
    orders.append(
        OrderRecord(
            event_sequence=ctx.event_sequence,
            order_index=index,
            decided_at=ctx.simulated_at,
            request=order,
            result=result,
        )
    )
    return result.account


async def run_strategy(spec: RunSpec, market: MarketPort, policy: DecisionPolicy) -> RunResult:
    """Run every scheduled event once. A failure stops the run but keeps what already settled."""
    schedule = build_schedule(spec.script)
    account: AccountSnapshot | None = None
    orders: list[OrderRecord] = []
    marks: list[MarkRecord] = []
    state, failure, code = RunState.RUNNING, None, None
    step = "while opening the run"

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
            at = {"event_sequence": event.event_sequence, "simulated_at": event.simulated_at}
            step = _at(event.kind.value, event.event_sequence, event.simulated_at)
            if event.kind is EventKind.MARK:
                with logfire.span("runner.mark", **at) as span:
                    await set_cutoff(event.simulated_at)
                    snapshot = await market.portfolio(ctx)
                    span.set_attribute("portfolio_value", str(snapshot.portfolio_value))
                marks.append(MarkRecord(event_sequence=event.event_sequence, snapshot=snapshot))
                continue
            with logfire.span("runner.decision", **at):
                await set_cutoff(event.simulated_at)
                account = await market.account(ctx)
                decision = await policy(ctx, account)
                for index, order in enumerate(decision):
                    account = await _submit(market, ctx, index, order, orders)
        state = RunState.COMPLETED
    except Exception as exc:  # noqa: BLE001 - any policy or market failure ends the run as failed
        state, code = RunState.FAILED, failure_code(exc)
        failure = describe_failure(exc, spec, step, account)

    if account is not None:
        try:
            account = await market.close_account(spec.experiment_id, account.account_id)
        except Exception as exc:  # noqa: BLE001 - keep the run's record even if closing fails
            state, code = RunState.FAILED, code or failure_code(exc)
            closing = describe_failure(exc, spec, "while closing the account", account)
            failure = f"{failure}; then {closing}" if failure else closing
            failure += f"; account {account.account_id} was left open"

    return RunResult(
        run_id=spec.run_id,
        state=state,
        account=account,
        orders=tuple(orders),
        marks=tuple(marks),
        failure=failure,
        failure_code=code,
    )
