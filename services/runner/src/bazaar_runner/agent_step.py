"""The agent places its own order; the runner reserves the id and reconciles with the market.

Per DECISION event: reserve client_order_id = uuid5(experiment_id, "decision:<event_sequence>"),
hand the agent a fresh HTTP client that carries only the approval and account headers, call the
decision once (never a retry, never a fresh id), and on any error or cancellation read the
account's orders from the market to learn what actually settled.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import NamedTuple
from uuid import UUID, uuid5

import httpx
from bazaar_protocol import AccountSnapshot, ExperimentContext, OrderRequest, OrderResult

from bazaar_runner.http_market import ACCOUNT_HEADER, APPROVAL_HEADER, market_client
from bazaar_runner.market import FiscalCycle, MarketPort
from bazaar_runner.run import (
    AgentUsage,
    DecisionError,
    DecisionStep,
    OrderRecord,
    ReconcileFailed,
    StepOutcome,
    order_record,
)

logger = logging.getLogger(__name__)

AGENT_TIMEOUT_SECONDS = 60.0


class AgentDecision(NamedTuple):
    order_request: OrderRequest | None
    order_result: OrderResult | None
    # A readable reason when the decision went wrong; None for a clean order or hold.
    error: str | None
    # As the agent harness reports it; None when it reported none (it raised, or a fake).
    usage: AgentUsage | None = None


# One symbol's fiscal cycle as of the decision's cutoff, or None (none, or the read failed).
CycleReader = Callable[[str], Awaitable[FiscalCycle | None]]
# (ctx, account, the agent's client, the reserved client_order_id, a cycle reader or None).
DecideWithAgent = Callable[
    [ExperimentContext, AccountSnapshot, httpx.AsyncClient, UUID, CycleReader | None],
    Awaitable[AgentDecision],
]


def reserved_order_id(ctx: ExperimentContext) -> UUID:
    """Deterministic per decision, so a resume or reconciliation reuses it."""
    return uuid5(ctx.experiment_id, f"decision:{ctx.event_sequence}")


def agent_headers(ctx: ExperimentContext) -> dict[str, str]:
    # Exactly these two. The runner token is a control-route credential and never reaches the agent.
    return {APPROVAL_HEADER: str(ctx.approval_id), ACCOUNT_HEADER: str(ctx.account_id)}


class _CycleReads:
    """The decision's fiscal-cycle reads: one symbol at a time, only when the agent asks for that
    symbol's filings, at this decision's cutoff (so never past it). The market caps a cycles
    request at 50 symbols; one symbol per read never comes near it. Failures are counted for the
    decision span: missing cycles make filings unsupported, never a failed decision."""

    def __init__(self, ctx: ExperimentContext, market: MarketPort) -> None:
        self._ctx, self._market = ctx, market
        self.found = self.absent = self.failed = 0

    async def __call__(self, symbol: str) -> FiscalCycle | None:
        try:
            cycles = await self._market.fiscal_cycles(self._ctx, (symbol,))
        except Exception as exc:  # noqa: BLE001 - never invent dates; record and go on
            self.failed += 1
            logger.warning(
                "fiscal cycle for %s unavailable at %s: %s",
                symbol,
                self._ctx.simulated_at,
                type(exc).__name__,
            )
            return None
        cycle = next((c for c in cycles if c.symbol == symbol), None)
        if cycle is None:
            self.absent += 1
        else:
            self.found += 1
        return cycle


def _cycle_attributes(cycles: _CycleReads | None) -> dict[str, str | int]:
    if cycles is None:
        return {"fiscal_cycles": "off"}
    return {
        "fiscal_cycles_found": cycles.found,
        "fiscal_cycles_absent": cycles.absent,
        "fiscal_cycles_failed": cycles.failed,
    }


class AgentStep(DecisionStep):
    def __init__(
        self,
        decide: DecideWithAgent,
        *,
        market_url: str,
        read_fiscal_cycles: bool = True,
        timeout: float = AGENT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._decide = decide
        self._read_fiscal_cycles = read_fiscal_cycles
        self._market_url = market_url
        self._timeout = timeout
        self._transport = transport

    async def decide(
        self,
        ctx: ExperimentContext,
        account: AccountSnapshot,
        market: MarketPort,
        orders: list[OrderRecord],
    ) -> StepOutcome:
        reserved = reserved_order_id(ctx)
        attributes: dict[str, str | bool | int] = {
            "agent": True,
            "client_order_id": str(reserved),
        }
        cycles = _CycleReads(ctx, market) if self._read_fiscal_cycles else None
        try:
            async with market_client(
                self._market_url,
                headers=agent_headers(ctx),
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                outcome = await self._decide(ctx, account, client, reserved, cycles)
                attributes |= _cycle_attributes(cycles)
        except asyncio.CancelledError:
            # Record what settled before propagating; the cancellation always wins.
            try:
                found = await self._find(ctx, market, reserved)
            except ReconcileFailed:
                logger.warning("reconcile after cancellation failed for %s", reserved)
            else:
                if found is not None:
                    orders.append(order_record(ctx, 0, found))
            raise
        except Exception as exc:  # noqa: BLE001 - an agent failure is reconciled, not trusted
            outcome = AgentDecision(None, None, f"the agent decision raised {type(exc).__name__}")
            attributes |= _cycle_attributes(cycles)
        result = outcome.order_result
        if outcome.error is None and result is not None and result.client_order_id != reserved:
            outcome = outcome._replace(error="the agent reported an order under another id")
        if outcome.error is None and result is None and outcome.order_request is not None:
            outcome = outcome._replace(error="the agent sent an order but reported no result")

        usage = outcome.usage
        if usage is not None:
            attributes |= {
                "model_requests": usage.model_requests,
                "tool_calls": usage.tool_calls,
                "total_tokens": usage.total_tokens,
            }
        if outcome.error is None:
            if outcome.order_result is not None:
                orders.append(order_record(ctx, 0, outcome.order_result))
            attributes["reconcile"] = "not_needed"
            return StepOutcome(usage=usage, attributes=attributes)

        # An error does not mean no side effects: the market's order list is the truth.
        found = await self._find(ctx, market, reserved)
        if found is not None:
            orders.append(order_record(ctx, 0, found))
        reconciled = "found" if found is not None else "absent"
        error = DecisionError(
            event_sequence=ctx.event_sequence,
            decided_at=ctx.simulated_at,
            client_order_id=reserved,
            error=outcome.error,
            reconciled=reconciled,
            usage=usage,
        )
        return StepOutcome(
            error=error, usage=usage, attributes=attributes | {"reconcile": reconciled}
        )

    async def _find(
        self, ctx: ExperimentContext, market: MarketPort, reserved: UUID
    ) -> OrderResult | None:
        try:
            results = await market.orders(ctx, ctx.simulated_at)
        except Exception as exc:  # noqa: BLE001 - surfaced as reconcile_failed
            raise ReconcileFailed(f"order list unavailable: {type(exc).__name__}") from None
        return next((r for r in results if r.client_order_id == reserved), None)
