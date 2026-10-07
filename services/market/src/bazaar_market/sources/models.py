from __future__ import annotations

from datetime import date

from pydantic import AwareDatetime, BaseModel, ConfigDict


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class Membership(_Record):
    """One spell during which a ticker belonged to the universe. `end` is the first day outside.

    A spell is keyed by the ticker in use at the time, so a renamed company has one per ticker.
    """

    ticker: str
    start: date
    end: date | None


class Filing(_Record):
    """One SEC filing. An amendment is its own filing with its own acceptance time."""

    cik: int
    accession: str
    form: str
    report_date: date | None
    filing_date: date
    accepted_at: AwareDatetime
    primary_document: str
    items: tuple[str, ...] = ()


class Fact(_Record):
    """One reported number, tied to the filing that reported it."""

    cik: int
    taxonomy: str
    concept: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    fiscal_year: int | None
    fiscal_period: str | None
    form: str
    accession: str
    filed: date


class NewsItem(_Record):
    source: str
    id: str
    symbols: tuple[str, ...]
    headline: str
    body: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    summary: str = ""

    @property
    def has_body(self) -> bool:
        return bool(self.body.strip())
