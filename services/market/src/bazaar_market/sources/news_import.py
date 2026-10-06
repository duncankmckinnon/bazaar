"""Import a frozen Alpaca news snapshot into the market database.

Each article is published at `created_at`. Alpaca keeps only the latest revision, so the text on
file is revised and available at `updated_at`, or at `created_at` if that is later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..archive import truncate
from ..news import HEADLINE_LIMIT, NEWS_VERSION, TEXT_LIMIT, NewsRecord, import_news
from .errors import SourceError
from .models import NewsItem
from .read import load_news, news_coverage, recorded_windows


@dataclass(frozen=True)
class SymbolNews:
    symbol: str
    articles: int
    without_headline: int


@dataclass(frozen=True)
class NewsImportReport:
    symbols: list[SymbolNews]
    left_out: tuple[str, ...]


def to_record(symbol: str, item: NewsItem) -> NewsRecord:
    return NewsRecord(
        symbol=symbol,
        record_id=item.id,
        revision=item.updated_at.isoformat(),
        published_at=item.created_at,
        available_at=max(item.created_at, item.updated_at),
        headline=truncate(item.headline.strip(), HEADLINE_LIMIT),
        text=truncate(item.body, TEXT_LIMIT),
    )


def import_news_snapshot(
    connection: sqlite3.Connection,
    snapshot_dir: Path,
    *,
    data_version: str = NEWS_VERSION,
    expected: tuple[str, ...] | None = None,
    symbols: tuple[str, ...] | None = None,
) -> NewsImportReport:
    """Check that every wanted symbol was fetched completely, then store its articles.

    `expected` lists the symbols the fetch was meant to cover, usually the config's news
    symbols. `symbols`, when given, replaces it and the other fetched symbols are left out.
    An article with an empty headline cannot be served and is counted, not stored.
    """
    snapshot_dir = Path(snapshot_dir)
    fetched = recorded_windows(snapshot_dir)
    if not fetched:
        raise SourceError(f"{snapshot_dir} records no news windows")
    wanted = symbols if symbols is not None else expected if expected is not None else fetched
    never = sorted(set(wanted) - set(fetched))
    if never:
        raise SourceError(f"missing coverage: {snapshot_dir} never fetched {', '.join(never)}")

    records: list[NewsRecord] = []
    coverage = {}
    report = []
    for symbol in sorted(set(wanted)):
        coverage[symbol] = news_coverage(snapshot_dir, symbol)
        items = load_news(snapshot_dir, symbol)
        kept = [to_record(symbol, item) for item in items if item.headline.strip()]
        records += kept
        report.append(SymbolNews(symbol, len(kept), len(items) - len(kept)))
    import_news(connection, records, coverage, data_version=data_version)
    return NewsImportReport(report, tuple(sorted(set(fetched) - set(wanted))))
