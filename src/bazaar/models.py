from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class Commodity(BaseModel):
    symbol: str
    unit: str = "ton"


class Quote(BaseModel):
    """A market reference price at a point in time."""

    commodity: Commodity
    price: float = Field(gt=0, description="Price per unit")
    as_of: datetime = Field(default_factory=_now)


class Motive(BaseModel):
    """Private incentives an agent negotiates under; never shared with the counterparty."""

    quantity_target: float = Field(description="Units to sell (quota) or acquire (need)")
    quantity_done: float = 0.0
    urgency: float = Field(default=0.5, ge=0, le=1, description="0 = relaxed, 1 = desperate")

    @property
    def remaining(self) -> float:
        return max(self.quantity_target - self.quantity_done, 0.0)


class Offer(BaseModel):
    by: Literal["seller", "buyer"]
    quantity: float = Field(gt=0)
    price: float = Field(gt=0, description="Price per unit")
    message: str = ""


class Deal(BaseModel):
    commodity: Commodity
    quantity: float
    price: float
    market_price: float
    rounds: int
    closed_at: datetime = Field(default_factory=_now)


class Outcome(BaseModel):
    """Result of one negotiation: a deal, or None if someone walked away."""

    deal: Deal | None = None
    transcript: list[Offer] = Field(default_factory=list)
