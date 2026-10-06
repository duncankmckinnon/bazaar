"""Deterministic baseline policies that run through the same market execution as candidates."""

from collections.abc import Sequence
from typing import Protocol
from uuid import uuid5

from bazaar_protocol import AccountSnapshot, ExperimentContext, OrderRequest, PriceObservation
from pydantic import AwareDatetime

# Decision, PriceAt and DecisionPolicy mirror bazaar_runner.policy (feat/experiment-runner c208eea)
# so that switching to the runner's types is an import change.
Decision = tuple[OrderRequest, ...]


class PriceAt(Protocol):
    """As-of price lookup, bound to the market port and injected into a policy at construction."""

    async def __call__(self, symbol: str, cutoff: AwareDatetime) -> PriceObservation: ...


class DecisionPolicy(Protocol):
    """Called once per DECISION event; orders fill at ctx.simulated_at."""

    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision: ...


class CashOnly:
    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision:
        return ()


class BuyAndHold:
    """Equal-weight whole-share basket bought at the first decision, then held.

    Sizing assumes zero fees (fixture rule). Use one instance per run.
    """

    def __init__(self, symbols: Sequence[str], prices: PriceAt) -> None:
        self.symbols = tuple(symbols)
        self.prices = prices
        self.bought = False

    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision:
        if self.bought:
            return ()

        cash = account.cash
        orders = []
        for remaining, symbol in zip(range(len(self.symbols), 0, -1), self.symbols, strict=True):
            observation = await self.prices(symbol, ctx.simulated_at)
            if observation.available_at > ctx.simulated_at:
                raise ValueError(f"{symbol} price is not available at {ctx.simulated_at}")
            price = observation.price
            quantity = cash // (price * remaining)
            if quantity == 0:
                continue
            cash -= quantity * price
            orders.append(
                OrderRequest(
                    client_order_id=uuid5(ctx.experiment_id, symbol),
                    symbol=symbol,
                    side="buy",
                    quantity=quantity,
                )
            )
        # Only a completed decision counts; a failed one is retried with the same order ids.
        self.bought = True
        return tuple(orders)
