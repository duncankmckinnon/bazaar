"""Deterministic baseline policies that run through the same market execution as candidates."""

from collections.abc import Callable, Sequence
from typing import Protocol
from uuid import uuid5

from bazaar_protocol import (
    AccountSnapshot,
    ExperimentContext,
    OrderRequest,
    PriceObservation,
    Symbol,
)
from pydantic import AwareDatetime

# Latest observation with available_at <= cutoff, shaped like the market's MarketData.price_at.
PriceLookup = Callable[[Symbol, AwareDatetime], PriceObservation]


class DecisionPolicy(Protocol):
    """Local stand-in for bazaar_runner's decide()/Decision; swap to it when the runner lands."""

    async def decide(
        self, ctx: ExperimentContext, account: AccountSnapshot
    ) -> tuple[OrderRequest, ...]: ...


class CashOnly:
    async def decide(
        self, ctx: ExperimentContext, account: AccountSnapshot
    ) -> tuple[OrderRequest, ...]:
        return ()


class BuyAndHold:
    """Equal-weight whole-share basket bought at the first decision, then held.

    Sizing assumes zero fees (fixture rule). Use one instance per run.
    """

    def __init__(self, symbols: Sequence[Symbol], prices: PriceLookup) -> None:
        self.symbols = tuple(symbols)
        self.prices = prices
        self.bought = False

    async def decide(
        self, ctx: ExperimentContext, account: AccountSnapshot
    ) -> tuple[OrderRequest, ...]:
        if self.bought:
            return ()
        self.bought = True

        cash = account.cash
        orders = []
        for remaining, symbol in zip(range(len(self.symbols), 0, -1), self.symbols, strict=True):
            price = self.prices(symbol, ctx.simulated_at).price
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
        return tuple(orders)
