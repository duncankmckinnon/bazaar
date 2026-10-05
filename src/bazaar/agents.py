from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import Motive, Offer, Quote
from .strategy import Strategy


@dataclass
class Agent:
    """Rule-based baseline agent. Swap `respond` for an LLM-backed implementation later."""

    role: Literal["seller", "buyer"]
    motive: Motive
    strategy: Strategy = field(default_factory=Strategy)

    def opening(self, quote: Quote) -> Offer:
        sign = 1 if self.role == "seller" else -1
        price = quote.price * (1 + sign * self.strategy.opening_markup)
        return Offer(by=self.role, quantity=self.motive.remaining, price=price)

    def respond(
        self, quote: Quote, last: Offer, own_last: Offer
    ) -> Offer | Literal["accept", "walk"]:
        sign = 1 if self.role == "seller" else -1
        limit = quote.price * (1 - sign * self.strategy.walk_away_margin)
        acceptable = last.price >= limit if self.role == "seller" else last.price <= limit
        if acceptable:
            return "accept"
        gap = last.price - own_last.price
        counter = own_last.price + gap * self.strategy.concession_rate
        return Offer(by=self.role, quantity=last.quantity, price=counter)


Seller = Buyer = Agent
