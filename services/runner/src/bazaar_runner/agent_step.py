"""The agent places its own order; the runner reserves the id and reconciles with the market.

Per DECISION event: reserve client_order_id = uuid5(experiment_id, "decision:<event_sequence>"),
hand the agent a fresh HTTP client that carries only the approval and account headers, call the
decision once (never a retry, never a fresh id), and on any error or cancellation read the
account's orders from the market to learn what actually settled.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import NamedTuple
from uuid import UUID, uuid5

import httpx
from bazaar_protocol import AccountSnapshot, ExperimentContext, OrderRequest, OrderResult

from bazaar_runner.http_market import ACCOUNT_HEADER, APPROVAL_HEADER
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


# (ctx, account, the agent's client, the reserved client_order_id, fiscal cycles as of ctx).
DecideWithAgent = Callable[
    [ExperimentContext, AccountSnapshot, httpx.AsyncClient, UUID, tuple[FiscalCycle, ...]],
    Awaitable[AgentDecision],
]


def reserved_order_id(ctx: ExperimentContext) -> UUID:
    """Deterministic per decision, so a resume or reconciliation reuses it."""
    return uuid5(ctx.experiment_id, f"decision:{ctx.event_sequence}")


def agent_headers(ctx: ExperimentContext) -> dict[str, str]:
    # Exactly these two. The runner token is a control-route credential and never reaches the agent.
    return {APPROVAL_HEADER: str(ctx.approval_id), ACCOUNT_HEADER: str(ctx.account_id)}


class AgentStep(DecisionStep):
    def __init__(
        self,
        decide: DecideWithAgent,
        *,
        market_url: str,
        symbols: Sequence[str] = (),
        read_fiscal_cycles: bool = True,
        timeout: float = AGENT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._decide = decide
        self._symbols = tuple(symbols)
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
        cycles, read = await self._fiscal_cycles(ctx, market)
        attributes |= {"fiscal_cycles": len(cycles), "fiscal_cycles_read": read}
        try:
            async with httpx.AsyncClient(
                base_url=self._market_url,
                headers=agent_headers(ctx),
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                outcome = await self._decide(ctx, account, client, reserved, cycles)
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

    async def _fiscal_cycles(
        self, ctx: ExperimentContext, market: MarketPort
    ) -> tuple[tuple[FiscalCycle, ...], str]:
        """As of this decision's cutoff. Missing cycles make filings unsupported, not a failure."""
        if not self._read_fiscal_cycles or not self._symbols:
            return (), "off"
        try:
            return await market.fiscal_cycles(ctx, self._symbols), "ok"
        except Exception as exc:  # noqa: BLE001 - run on without cycles; never invent dates
            logger.warning(
                "fiscal cycles unavailable at %s: %s", ctx.simulated_at, type(exc).__name__
            )
            return (), "failed"

    async def _find(
        self, ctx: ExperimentContext, market: MarketPort, reserved: UUID
    ) -> OrderResult | None:
        try:
            results = await market.orders(ctx, ctx.simulated_at)
        except Exception as exc:  # noqa: BLE001 - surfaced as reconcile_failed
            raise ReconcileFailed(f"order list unavailable: {type(exc).__name__}") from None
        return next((r for r in results if r.client_order_id == reserved), None)
