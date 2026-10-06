"""What was readable at a simulated time. Callers pass the trusted clock, never an agent's value."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from .models import Fact, Filing, NewsItem


def _require_aware(as_of: datetime) -> None:
    if as_of.tzinfo is None:
        raise ValueError("as_of must carry a timezone")


def visible_filings(filings: list[Filing], as_of: datetime) -> list[Filing]:
    _require_aware(as_of)
    return sorted((f for f in filings if f.accepted_at <= as_of), key=lambda f: f.accepted_at)


def fact_available_at(fact: Fact, filings: list[Filing]) -> datetime:
    """A fact is readable once its filing was accepted.

    When the filing is not in hand, only the filing day is known, so wait for the next day.
    """
    for filing in filings:
        if filing.accession == fact.accession:
            return filing.accepted_at
    return datetime.combine(fact.filed + timedelta(days=1), time.min, tzinfo=UTC)


def visible_facts(facts: list[Fact], filings: list[Filing], as_of: datetime) -> list[Fact]:
    _require_aware(as_of)
    return [f for f in facts if fact_available_at(f, filings) <= as_of]


def visible_news(items: list[NewsItem], as_of: datetime) -> list[NewsItem]:
    """An article counts from its last revision, because only the revised text is on file."""
    _require_aware(as_of)
    return sorted((n for n in items if n.updated_at <= as_of), key=lambda n: n.updated_at)
