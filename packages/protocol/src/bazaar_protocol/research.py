"""Proposed research wire contracts, not implemented market endpoints."""

from datetime import date, datetime
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from bazaar_protocol import (
    AccountSnapshot,
    Cursor,
    OrderResult,
    PortfolioSnapshot,
    PriceHistoryRequest,
    Symbol,
    Version,
    WireModel,
)


class ResearchRequest(PriceHistoryRequest):
    """Bounded symbol/time query; no free-form search or query credentials."""


class HistoryRequest(WireModel):
    start_at: AwareDatetime
    end_at: AwareDatetime
    limit: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100
    cursor: Cursor | None = None


class Provenance(WireModel):
    source: Version
    data_version: Version
    record_id: Version
    revision: Version
    published_at: AwareDatetime
    available_at: AwareDatetime
    revised_at: AwareDatetime

    @field_validator("published_at", "revised_at", mode="before")
    @classmethod
    def iso_timestamp(cls, value: object) -> object:
        if not isinstance(value, str | datetime):
            raise ValueError("ISO timestamp required")  # noqa: TRY004
        if isinstance(value, str):
            datetime.fromisoformat(value)
        return value


class ArchivedNews(Provenance):
    symbol: Symbol
    headline: Annotated[str, Field(min_length=1, max_length=4096)]
    text: Annotated[str, Field(max_length=100_000)]


class CompanyFiling(Provenance):
    symbol: Symbol
    fiscal_period_start: date
    fiscal_period_end: date
    form: Version
    text: Annotated[str, Field(max_length=200_000)]


class PrivateRecord(Provenance):
    kind: Literal["note", "cache", "trace", "attempt"]
    simulated_at: AwareDatetime
    event_sequence: Annotated[int, Field(strict=True, ge=0)]
    text: Annotated[str, Field(max_length=100_000)]


T = TypeVar("T")


class HistoryPage[T](WireModel):
    experiment_id: UUID
    account_id: UUID
    agent_id: UUID
    strategy_version_id: UUID
    cutoff_at: AwareDatetime
    start_at: AwareDatetime
    end_at: AwareDatetime
    source: Version
    data_version: Version
    coverage: Literal["complete", "missing", "partial"]
    items: tuple[T, ...]
    next_cursor: Cursor | None = None


NewsPage = HistoryPage[ArchivedNews]
FilingPage = HistoryPage[CompanyFiling]
PrivateHistoryPage = HistoryPage[PrivateRecord]
AccountHistoryPage = HistoryPage[AccountSnapshot]
PortfolioHistoryPage = HistoryPage[PortfolioSnapshot]
OrderHistoryPage = HistoryPage[OrderResult]
