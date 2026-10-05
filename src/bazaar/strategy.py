from __future__ import annotations

from pydantic import BaseModel, Field


class Strategy(BaseModel):
    """Tunable negotiation criteria. Agents revise these from ledger history."""

    opening_markup: float = Field(
        0.10, description="Seller opens at market * (1 + x); buyer at market * (1 - x)"
    )
    concession_rate: float = Field(
        0.25, description="Fraction of the remaining gap conceded each round"
    )
    walk_away_margin: float = Field(
        0.05, description="Worst acceptable deviation from market price"
    )
    max_rounds: int = 6
