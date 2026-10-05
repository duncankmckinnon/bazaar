from __future__ import annotations

import random
from typing import Protocol

from .models import Commodity, Quote


class MarketFeed(Protocol):
    def quote(self, commodity: Commodity) -> Quote: ...


class SimulatedFeed:
    """Random-walk prices for local development and tests."""

    def __init__(
        self, start_prices: dict[str, float], volatility: float = 0.01, seed: int | None = None
    ):
        self._prices = dict(start_prices)
        self._volatility = volatility
        self._rng = random.Random(seed)

    def quote(self, commodity: Commodity) -> Quote:
        price = self._prices[commodity.symbol]
        price *= 1 + self._rng.gauss(0, self._volatility)
        self._prices[commodity.symbol] = price
        return Quote(commodity=commodity, price=price)
