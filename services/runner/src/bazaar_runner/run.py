"""Drive one approved strategy run over the scripted clock against a market port."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    Literal[
        "approval_denied",
        "future_data",
        "runner_unauthorized",
        "reconcile_failed",
        "period_overrun",
        "policy_error",
    ]
    | ErrorCode
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


class DecisionError(WireModel):
    """A decision that went wrong but was reconciled with the market, so the run went on."""

    event_sequence: Index
    decided_at: AwareDatetime
    client_order_id: UUID
    error: Annotated[str, Field(min_length=1)]
    # What the market's order list showed for the reserved id.
    reconciled: Literal["found", "absent"]


class PeriodOverrun(Exception):
    """The driver tried to move the market clock past the run's period_end; never sent."""


class ReconcileFailed(Exception):
    """The market's order list could not be read, so the account state is unknown (A9)."""


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
    decision_errors: tuple[DecisionError, ...] = ()


def failure_code(exc: Exception) -> FailureCode:
    if isinstance(exc, ApprovalDenied):
        return "approval_denied"
    if isinstance(exc, FutureData):
        return "future_data"
    if isinstance(exc, RunnerUnauthorized):
        return "runner_unauthorized"
    if isinstance(exc, ReconcileFailed):
        return "reconcile_failed"
    if isinstance(exc, PeriodOverrun):
        return "period_overrun"
    if isinstance(exc, MarketError):
        return exc.detail.code
    return "policy_error"


def describe_failure(
    exc: Exception, spec: "RunSpec", step: str, account: AccountSnapshot | None
) -> str:
    """One readable sentence for Logfire and the leaderboard: no exception repr, no secret.

    The leaderboard prints "<failure_code>: <failure>", so the sentence never repeats its code.
    """
    if isinstance(exc, ApprovalDenied):
        sentence = (
            f"approval {spec.approval_id} is not approved for experiment {spec.experiment_id}"
        )
        if account is None:
            sentence += "; refused before any account was opened"
        return sentence
    if isinstance(exc, RunnerUnauthorized):
        return f"the market refused the runner's credential {step}"
    if isinstance(exc, PeriodOverrun):
        return f"the runner stopped {step} instead of moving the clock past period_end ({exc})"
    if isinstance(exc, ReconcileFailed):
        return (
            f"the market's order list could not be read {step}, so the account state after"
            " the agent's decision is unknown"
        )
    if isinstance(exc, FutureData):
        return f"a read past the experiment's clock was refused {step}"
    if isinstance(exc, MarketError):
        # The adapter has already redacted the runner token from market messages.
        return f"{step}, the market said: {exc.detail.message}"
    return f"the run stopped on {type(exc).__name__} {step}"


def _at(kind: str, event_sequence: int, simulated_at: datetime) -> str:
    return f"during the {kind} at {utc_z(simulated_at)} (event {event_sequence})"


@dataclass(frozen=True)
class StepOutcome:
    error: DecisionError | None = None
    # Added to the runner.decision span; never a credential.
    attributes: Mapping[str, str | bool] = field(default_factory=dict)


class DecisionStep(ABC):
    """How one DECISION event turns into orders. The driver owns the clock, cutoffs and marks."""

    @abstractmethod
    async def decide(
        self,
        ctx: ExperimentContext,
        account: AccountSnapshot,
        market: MarketPort,
        orders: list[OrderRecord],
    ) -> StepOutcome:
        """Append each settled order to `orders` as soon as it settles, so a failure keeps it."""


class OrdersStep(DecisionStep):
    """A policy returns orders and the runner submits them: the scripted agent and baselines."""

    def __init__(self, policy: DecisionPolicy) -> None:
        self.policy = policy

    async def decide(self, ctx, account, market, orders) -> StepOutcome:
        for index, order in enumerate(await self.policy(ctx, account)):
            await submit_order(market, ctx, index, order, orders)
        return StepOutcome()


def order_record(ctx: ExperimentContext, index: int, result: OrderResult) -> OrderRecord:
    request = OrderRequest(
        client_order_id=result.client_order_id,
        symbol=result.symbol,
        side=result.side,
        quantity=result.quantity,
    )
    return OrderRecord(
        event_sequence=ctx.event_sequence,
        order_index=index,
        decided_at=ctx.simulated_at,
        request=request,
        result=result,
    )


async def submit_order(
    market: MarketPort,
    ctx: ExperimentContext,
    index: int,
    order: OrderRequest,
    orders: list[OrderRecord],
) -> None:
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


async def run_strategy(
    spec: RunSpec, market: MarketPort, decide: DecisionPolicy | DecisionStep
) -> RunResult:
    """Run every scheduled event once. A failure stops the run but keeps what already settled."""
    stepper = decide if isinstance(decide, DecisionStep) else OrdersStep(decide)
    schedule = build_schedule(spec.script)
    decision_errors: list[DecisionError] = []
    account: AccountSnapshot | None = None
    orders: list[OrderRecord] = []
    marks: list[MarkRecord] = []
    state, failure, code = RunState.RUNNING, None, None
    step = "while opening the run"

    period_end = spec.script.sessions[-1].close_at

    async def set_cutoff(cutoff: datetime) -> None:
        # The schedule ends at period_end; this guards the market clock against any driver bug.
        if cutoff > period_end:
            raise PeriodOverrun(f"{utc_z(cutoff)} is after {utc_z(period_end)}")
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
            with logfire.span("runner.decision", **at) as span:
                await set_cutoff(event.simulated_at)
                account = await market.account(ctx)
                settled = len(orders)
                try:
                    outcome = await stepper.decide(ctx, account, market, orders)
                finally:
                    if len(orders) > settled:
                        account = orders[-1].result.account
                for name, value in outcome.attributes.items():
                    span.set_attribute(name, value)
                if outcome.error is not None:
                    decision_errors.append(outcome.error)
                    span.set_attribute("decision_error", outcome.error.error)
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
        decision_errors=tuple(decision_errors),
    )
