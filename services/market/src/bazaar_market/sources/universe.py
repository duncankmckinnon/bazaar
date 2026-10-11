from __future__ import annotations

import csv
import io
import tomllib
from dataclasses import dataclass, field, replace
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
class Rename:
    """Bars fetched under `ticker` from `effective` on are stored as `symbol`, its earlier name.

    For a company renamed inside the window: the universe keeps the name it had when the universe
    was fixed, so agents see one symbol with an unbroken series.
    """

    ticker: str
    symbol: str
    effective: date


@dataclass(frozen=True)
class SourcesConfig:
    period_start: date
    period_end: date
    sp500_commit: str
    edgar_forms: tuple[str, ...]
    edgar_documents_since: date
    companies: tuple[Company, ...]
    edgar_user_agent: str | None = None
    # Optional [news] start: the news fetch begins here instead of period.start.
    news_start: date | None = None
    # [universe] benchmarks (e.g. SPY) and bars_only symbols get bars but no news or filings.
    benchmarks: tuple[str, ...] = ()
    bars_only: tuple[str, ...] = ()
    # The [universe] csv path, when the companies come from one (the v2 configs).
    universe_csv: Path | None = None
    # [universe] renames: tickers fetched for bars and stored under a universe symbol.
    renames: tuple[Rename, ...] = ()
    # [universe] news_aliases inverted: a news tag fetched in place of a symbol -> that symbol.
    news_aliases: dict[str, str] = field(default_factory=dict)

    @property
    def bar_tickers(self) -> tuple[str, ...]:
        """Every ticker the bars fetch requests: the companies, then benchmarks and bars_only."""
        return tuple(
            dict.fromkeys(
                [
                    *(c.ticker for c in self.companies),
                    *self.benchmarks,
                    *self.bars_only,
                    *(r.ticker for r in self.renames),
                ]
            )
        )


def _section(raw: dict, name: str) -> dict:
    if not isinstance(raw.get(name), dict):
        raise ConfigError(f"missing [{name}] section")
    return raw[name]


def _date(section: dict, name: str, key: str) -> date:
    value = section.get(key)
    if not isinstance(value, date):
        raise ConfigError(f"{name}.{key} must be a TOML date such as 2022-06-01, got {value!r}")
    return value


def load_config(path: Path, universe_csv: Path | None = None) -> SourcesConfig:
    """Load a sources config. Every setting is type-checked, and only edgar.user_agent is optional.

    `universe_csv` replaces the config's [universe] csv. The real constituent list is not
    committed, so it is a local file named at build time.
    """
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

    universe = raw.get("universe")
    if universe is not None:
        if raw.get("company"):
            raise ConfigError("give companies in [universe] csv or as [[company]], not both")
        aliases = _news_aliases(universe)
        companies = [
            replace(c, news_symbols=tuple(aliases.get(c.ticker, c.news_symbols)))
            for c in _universe_companies(_csv_path(path, universe, universe_csv))
        ]
        unknown = sorted(set(aliases) - {c.ticker for c in companies})
        if unknown:
            raise ConfigError(f"universe.news_aliases names symbols not in the universe: {unknown}")
    else:
        companies = _listed_companies(raw)
    news = raw.get("news", {})
    news_start = _date(news, "news", "start") if "start" in news else None
    if news_start is not None and not start <= news_start <= end:
        raise ConfigError(f"news.start {news_start} is outside the period")
    return SourcesConfig(
        period_start=start,
        period_end=end,
        sp500_commit=sp500["commit"],
        edgar_forms=tuple(forms),
        edgar_documents_since=_date(edgar, "edgar", "documents_since"),
        companies=tuple(companies),
        edgar_user_agent=user_agent,
        news_start=news_start,
        benchmarks=_symbol_list(universe or {}, "benchmarks"),
        bars_only=_symbol_list(universe or {}, "bars_only"),
        universe_csv=_csv_path(path, universe, universe_csv) if universe is not None else None,
        renames=_renames(universe or {}, {c.ticker for c in companies}),
        news_aliases={
            tag: symbol for symbol, tags in _news_aliases(universe or {}).items() for tag in tags
        },
    )


def _news_aliases(universe: dict) -> dict[str, list[str]]:
    aliases = universe.get("news_aliases", {})
    if not isinstance(aliases, dict) or not all(
        isinstance(tags, list) and tags and all(isinstance(t, str) and t for t in tags)
        for tags in aliases.values()
    ):
        raise ConfigError("universe.news_aliases maps a symbol to a list of news tags")
    tags = [t for ts in aliases.values() for t in ts]
    if len(tags) != len(set(tags)):
        raise ConfigError("universe.news_aliases uses a news tag for two symbols")
    return aliases


def _renames(universe: dict, symbols: set[str]) -> tuple[Rename, ...]:
    entries = universe.get("renames", [])
    if not isinstance(entries, list):
        raise ConfigError("universe.renames must be a list of {ticker, symbol, effective}")
    renames = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"ticker", "symbol", "effective"}:
            raise ConfigError(
                f"universe.renames entry needs ticker, symbol and effective: {entry!r}"
            )
        if entry["symbol"] not in symbols:
            raise ConfigError(f"universe.renames maps {entry['ticker']} to {entry['symbol']}, "
                              "which is not in the universe")  # fmt: skip
        effective = _date(entry, "universe.renames", "effective")
        renames.append(Rename(entry["ticker"], entry["symbol"], effective))
    return tuple(renames)


UNIVERSE_COLUMNS = ["symbol", "name", "gics_sector", "cik"]


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    sector: str
    cik: int


def read_universe(path: Path) -> list[UniverseRow]:
    """A universe CSV: symbol,name,gics_sector,cik. Every field is required; nothing is guessed."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != UNIVERSE_COLUMNS:
            raise ConfigError(f"{path}: the header must be {','.join(UNIVERSE_COLUMNS)}")
        rows, seen = [], set()
        for line, cells in enumerate(reader, start=2):
            if len(cells) != 4 or not all(c.strip() for c in cells):
                raise ConfigError(f"{path}:{line}: every row needs a symbol, name, sector and CIK")
            symbol, name, sector, cik = (c.strip() for c in cells)
            if symbol in seen:
                raise ConfigError(f"{path}:{line}: {symbol} is listed twice")
            if not cik.isdigit():
                raise ConfigError(f"{path}:{line}: {symbol} CIK must be a number, got {cik!r}")
            seen.add(symbol)
            rows.append(UniverseRow(symbol, name, sector, int(cik)))
    if not rows:
        raise ConfigError(f"{path} lists no companies")
    return rows


def _csv_path(config: Path, universe: dict, override: Path | None) -> Path:
    if override is not None:
        return Path(override)
    if not isinstance(universe.get("csv"), str):
        raise ConfigError("universe.csv must be the path of the universe CSV")
    return Path(config).parent / universe["csv"]


def _universe_companies(csv_path: Path) -> list[Company]:
    if not csv_path.is_file():
        raise ConfigError(
            f"no universe CSV at {csv_path}. It is not committed: build it as "
            "config/sp500-2026-09-25.md describes, or pass --universe-csv"
        )
    rows = read_universe(csv_path)
    return [Company(ticker=r.symbol, cik=r.cik, news_symbols=(r.symbol,)) for r in rows]


def _symbol_list(section: dict, key: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"universe.{key} must be a list of tickers, got {value!r}")
    return tuple(value)


def _listed_companies(raw: dict) -> list[Company]:
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
    return companies


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
