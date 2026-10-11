"""Import a frozen Alpaca news snapshot into the market database.

Each article is published at `created_at`. Alpaca keeps only the latest revision, so the text on
file is revised and available at `updated_at`, or at `created_at` if that is later.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..archive import html_to_text, truncate
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


def article_text(item: NewsItem) -> str:
    """The article's content as plain text, or its summary when it has no content."""
    content = html_to_text(item.body)
    return content if content else html_to_text(item.summary)


def to_record(symbol: str, item: NewsItem) -> NewsRecord:
    return NewsRecord(
        symbol=symbol,
        record_id=item.id,
        revision=item.updated_at.isoformat(),
        published_at=item.created_at,
        available_at=max(item.created_at, item.updated_at),
        headline=truncate(item.headline.strip(), HEADLINE_LIMIT),
        text=truncate(article_text(item), TEXT_LIMIT),
    )


def import_news_snapshot(
    connection: sqlite3.Connection,
    snapshot_dir: Path,
    *,
    data_version: str = NEWS_VERSION,
    expected: tuple[str, ...] | None = None,
    symbols: tuple[str, ...] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> NewsImportReport:
    """Check that every wanted symbol was fetched completely, then store its articles.

    `expected` lists the symbols the fetch was meant to cover, usually the config's news
    symbols. `symbols`, when given, replaces it and the other fetched symbols are left out.
    An article with an empty headline cannot be served and is counted, not stored.
    `aliases` maps a fetched news tag to the universe symbol its articles are stored under
    (Alpaca tags Berkshire's class B as BRK/B and BRK, not BRK.B). An article found under two
    tags of one symbol is stored once, and the symbol's window is the part all its tags cover.
    """
    aliases = aliases or {}
    snapshot_dir = Path(snapshot_dir)
    fetched = recorded_windows(snapshot_dir)
    if not fetched:
        raise SourceError(f"{snapshot_dir} records no news windows")
    wanted = symbols if symbols is not None else expected if expected is not None else fetched
    never = sorted(set(wanted) - set(fetched))
    if never:
        raise SourceError(f"missing coverage: {snapshot_dir} never fetched {', '.join(never)}")

    by_symbol: dict[str, list[str]] = {}
    for tag in sorted(set(wanted)):
        by_symbol.setdefault(aliases.get(tag, tag), []).append(tag)
    records: list[NewsRecord] = []
    coverage = {}
    report = []
    for symbol, tags in sorted(by_symbol.items()):
        windows = [news_coverage(snapshot_dir, tag) for tag in tags]
        coverage[symbol] = (max(w[0] for w in windows), min(w[1] for w in windows))
        items = {item.id: item for tag in tags for item in load_news(snapshot_dir, tag)}
        kept = [to_record(symbol, item) for item in items.values() if item.headline.strip()]
        records += kept
        report.append(SymbolNews(symbol, len(kept), len(items) - len(kept)))
    import_news(connection, records, coverage, data_version=data_version)
    return NewsImportReport(report, tuple(sorted(set(fetched) - set(wanted))))
