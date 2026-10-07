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
    edgar_user_agent: str | None = None


def _section(raw: dict, name: str) -> dict:
    if not isinstance(raw.get(name), dict):
        raise ConfigError(f"missing [{name}] section")
    return raw[name]


def _date(section: dict, name: str, key: str) -> date:
    value = section.get(key)
    if not isinstance(value, date):
        raise ConfigError(f"{name}.{key} must be a TOML date such as 2022-06-01, got {value!r}")
    return value


def load_config(path: Path) -> SourcesConfig:
    """Load a sources config. Every setting is type-checked, and only edgar.user_agent is optional."""
    raw = tomllib.loads(Path(path).read_text())
    period, sp500, edgar = _section(raw, "period"), _section(raw, "sp500"), _section(raw, "edgar")
    start, end = _date(period, "period", "start"), _date(period, "period", "end")
    if end < start:
        raise ConfigError(f"period.end {end} is before period.start {start}")
    forms = edgar.get("forms")
    if not isinstance(forms, list) or not all(isinstance(f, str) for f in forms):
        raise ConfigError(f"edgar.forms must be a list of form names, got {forms!r}")
    user_agent = edgar.get("user_agent")
    if user_agent is not None and not isinstance(user_agent, str):
        raise ConfigError(f"edgar.user_agent must be a string, got {user_agent!r}")
    if not isinstance(sp500.get("commit"), str):
        raise ConfigError("sp500.commit must be a commit hash")

    companies: list[Company] = []
    for entry in raw.get("company", []):
        ticker = entry.get("ticker")
        if not ticker:
            raise ConfigError("every [[company]] needs a ticker")
        if any(c.ticker == ticker for c in companies):
            raise ConfigError(f"{ticker} is listed twice")
        if not isinstance(entry.get("cik"), int):
            raise ConfigError(f"{ticker} has no cik. EDGAR cannot be queried by ticker.")
        symbols = tuple(entry.get("news_symbols", [ticker]))
        companies.append(Company(ticker=ticker, cik=entry["cik"], news_symbols=symbols))
    if not companies:
        raise ConfigError("the config names no [[company]]")
    return SourcesConfig(
        period_start=start,
        period_end=end,
        sp500_commit=sp500["commit"],
        edgar_forms=tuple(forms),
        edgar_documents_since=_date(edgar, "edgar", "documents_since"),
        companies=tuple(companies),
        edgar_user_agent=user_agent,
    )


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


def in_universe(memberships: list[Membership], ticker: str, on: date) -> bool:
    """Whether `ticker` was a member on `on`. The end date of a spell is already outside.

    This is index membership, not trading status. The membership file can end a spell days
    after the last trade, and it does not record halts.
    """
    return any(
        m.ticker == ticker and m.start <= on and (m.end is None or on < m.end) for m in memberships
    )
