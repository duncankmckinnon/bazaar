from __future__ import annotations

import csv
import io
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import Membership


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: int
    news_symbols: tuple[str, ...]


@dataclass(frozen=True)
class SourcesConfig:
    period_start: date
    period_end: date
    sp500_commit: str
    edgar_forms: tuple[str, ...]
    edgar_documents_since: date
    companies: tuple[Company, ...]


def load_config(path: Path) -> SourcesConfig:
    raw = tomllib.loads(Path(path).read_text())
    companies = []
    for entry in raw.get("company", []):
        ticker = entry.get("ticker")
        if not ticker:
            raise ConfigError("every [[company]] needs a ticker")
        if "cik" not in entry:
            raise ConfigError(f"{ticker} has no cik. EDGAR cannot be queried by ticker.")
        symbols = tuple(entry.get("news_symbols", [ticker]))
        companies.append(Company(ticker=ticker, cik=int(entry["cik"]), news_symbols=symbols))
    try:
        return SourcesConfig(
            period_start=raw["period"]["start"],
            period_end=raw["period"]["end"],
            sp500_commit=raw["sp500"]["commit"],
            edgar_forms=tuple(raw["edgar"]["forms"]),
            edgar_documents_since=raw["edgar"]["documents_since"],
            companies=tuple(companies),
        )
    except KeyError as exc:
        raise ConfigError(f"missing required setting: {exc}") from exc


def parse_sp500_start_end(text: str) -> list[Membership]:
    """Parse `sp500_ticker_start_end.csv` from fja05680/sp500: ticker, start_date, end_date."""
    return [
        Membership(
            ticker=row["ticker"],
            start=date.fromisoformat(row["start_date"]),
            end=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
        )
        for row in csv.DictReader(io.StringIO(text))
    ]


def tradable(memberships: list[Membership], ticker: str, on: date) -> bool:
    return any(
        m.ticker == ticker and m.start <= on and (m.end is None or on < m.end) for m in memberships
    )
