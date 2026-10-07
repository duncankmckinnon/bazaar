"""Run the fetchers for everything a sources config names."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import httpx

from .alpaca_bars import fetch_bars, ticker_windows
from .alpaca_news import fetch_news
from .edgar import EdgarClient
from .errors import SourceError
from .http import get
from .models import Membership
from .snapshot import Snapshot
from .universe import SourcesConfig, parse_sp500_start_end

SP500_URL = "https://raw.githubusercontent.com/fja05680/sp500/{commit}/sp500_ticker_start_end.csv"


def fetch_universe(cfg: SourcesConfig, root: Path, http: httpx.Client) -> list[Membership]:
    url = SP500_URL.format(commit=cfg.sp500_commit)
    response = get(http, url)
    if response.status_code != 200:
        raise SourceError(f"membership file returned {response.status_code} for {url}")
    members = parse_sp500_start_end(response.text)
    known = {m.ticker for m in members}
    missing = [c.ticker for c in cfg.companies if c.ticker not in known]
    if missing:
        raise SourceError(f"no membership history for: {', '.join(missing)}")
    snap = Snapshot(root, source="sp500", version=cfg.sp500_commit[:7])
    snap.write("sp500_ticker_start_end.csv", response.content, url=url, rows=len(members))
    return members


def fetch_edgar(
    cfg: SourcesConfig,
    root: Path,
    http: httpx.Client,
    *,
    version: str,
    user_agent: str,
    min_interval: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, int]]:
    """Freeze filing history and numbers for each company, plus filing text when allowed.

    Filing text needs a contact address in `user_agent`. Without one it is skipped, not faked.
    """
    client = EdgarClient(http, user_agent=user_agent, min_interval=min_interval, sleep=sleep)
    snap = Snapshot(root, source="edgar", version=version)
    summary = {}
    for company in cfg.companies:
        filings = client.fetch_company(company.cik, snap)
        documents = 0
        if "@" in user_agent:
            for filing in filings:
                wanted = (
                    filing.form in cfg.edgar_forms
                    and cfg.edgar_documents_since <= filing.accepted_at.date() <= cfg.period_end
                    and filing.primary_document
                )
                if wanted:
                    client.fetch_document(filing, snap)
                    documents += 1
        summary[company.ticker] = {"filings": len(filings), "documents": documents}
    return summary


def fetch_news_range(
    cfg: SourcesConfig,
    root: Path,
    http: httpx.Client,
    *,
    version: str,
    start: date,
    end: date,
    headers: dict[str, str] | None = None,
    until: datetime | None = None,
) -> dict[str, int]:
    snap = Snapshot(root, source="alpaca-news", version=version)
    summary = {}
    for company in cfg.companies:
        for symbol in company.news_symbols:
            items = fetch_news(
                http, symbol=symbol, start=start, end=end, snap=snap, headers=headers, until=until
            )
            summary[symbol] = len(items)
    return summary


def fetch_bars_range(
    cfg: SourcesConfig,
    root: Path,
    http: httpx.Client,
    *,
    version: str,
    feed: str,
    headers: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Freeze unadjusted daily bars for every configured company over the configured period."""
    snap = Snapshot(root, source="alpaca-bars", version=version)
    tickers = tuple(c.ticker for c in cfg.companies)
    summary = {}
    for window in ticker_windows(tickers, cfg.period_start, cfg.period_end):
        bars = fetch_bars(http, window=window, snap=snap, feed=feed, headers=headers, sleep=sleep)
        summary[window.ticker] = len(bars)
    return summary
