from __future__ import annotations

from .ledger import Ledger


def avg_premium_to_market(ledger: Ledger) -> float | None:
    """Mean (price / market_price - 1) across all deals. Positive favours the seller."""
    ((value,),) = ledger.query("SELECT AVG(price / market_price - 1) FROM deals")
    return value


def deal_count(ledger: Ledger) -> int:
    ((n,),) = ledger.query("SELECT COUNT(*) FROM deals")
    return n
