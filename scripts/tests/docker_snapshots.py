"""CI-only synthetic source snapshots, piped into the Docker downloader image.

Never use these for research. Require a distinct snapshot name so fixtures cannot
silently overwrite or stand in for the real demo download.
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path

from bazaar_market.sources.alpaca_bars import ticker_windows
from bazaar_market.sources.snapshot import Snapshot
from bazaar_market.sources.universe import load_config

assert os.environ["BAZAAR_SNAPSHOT_VERSION"] == "docker-smoke"
config = load_config(Path("config/demo-sources.toml"))
root = Path("/snapshots")
bars = Snapshot(root, source="alpaca-bars", version="docker-smoke")
news = Snapshot(root, source="alpaca-news", version="docker-smoke")
edgar = Snapshot(root, source="edgar", version="docker-smoke")
days = [date(2026, 1, 30) + timedelta(days=n) for n in range(15)]
days = [day for day in days if day.weekday() < 5]
tickers = tuple(company.ticker for company in config.companies)
for window in ticker_windows(tickers, config.period_start, config.period_end):
    ticker = window.ticker
    bars.cover(
        ticker,
        start="2026-01-30T05:00:00Z",
        end="2026-02-14T04:59:59Z",
        feed="sip",
        adjustment="raw",
    )
    rows = [
        {"t": f"{day}T05:00:00Z", "o": 100, "h": 102, "l": 99, "c": 101, "v": 1000} for day in days
    ]
    bars.write(
        f"{ticker}/page-0001.json",
        json.dumps({"bars": {ticker: rows}, "next_page_token": None}).encode(),
        url="https://example.invalid/ci-only-synthetic-bars",
        rows=len(rows),
    )

for company in config.companies:
    for symbol in company.news_symbols:
        news.cover(symbol, start="2025-07-01T00:00:00Z", end="2026-09-30T23:59:59Z")
        news.write(
            f"{symbol}/page-0001.json",
            json.dumps({"news": [], "next_page_token": None}).encode(),
            url="https://example.invalid/ci-only-empty-news",
            rows=0,
        )
    recent = {
        key: []
        for key in (
            "accessionNumber",
            "form",
            "reportDate",
            "filingDate",
            "acceptanceDateTime",
            "primaryDocument",
            "items",
        )
    }
    for kind, payload in (
        ("submissions", {"cik": str(company.cik), "filings": {"recent": recent, "files": []}}),
        ("companyfacts", {"cik": company.cik, "facts": {}}),
    ):
        edgar.write(
            f"{kind}/CIK{company.cik:010d}.json",
            json.dumps(payload).encode(),
            url="https://example.invalid/ci-only-empty-filings",
            rows=0,
        )
print("Created CI-only docker-smoke snapshots (not real market data).")
