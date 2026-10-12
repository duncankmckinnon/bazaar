"""CI-only synthetic source snapshots and universe, piped into the Docker downloader image.

Never use these for research. Every value is made up: three symbols (AAPL, so the smoke run has a
familiar ticker to buy, plus two invented ones) and SPY, with invented names and CIKs. The data is
shaped like demo-bundle-v2: bars on every session from 2026-09-01 to 2026-10-09, one news article
per symbol, and one 10-Q per company. A distinct snapshot name keeps it from standing in for a real
download. `write(root)` is also used offline by scripts/tests/test_docker_snapshots.py.
"""

import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bazaar_market.sources.snapshot import Snapshot

VERSION = "docker-smoke"
EASTERN = ZoneInfo("America/New_York")
UNIVERSE = [
    ("AAPL", "Synthetic Apple Co.", "Information Technology", 9000001),
    ("AAAA", "Example Industries", "Industrials", 9000002),
    ("BBBB", "Example Holdings", "Financials", 9000003),
]
BENCHMARK = "SPY"
START, END = date(2026, 9, 1), date(2026, 10, 9)
LABOR_DAY = date(2026, 9, 7)
SESSIONS = [
    START + timedelta(days=n)
    for n in range((END - START).days + 1)
    if (START + timedelta(days=n)).weekday() < 5 and START + timedelta(days=n) != LABOR_DAY
]


def _bar_time(day: date) -> str:
    """Alpaca stamps a daily bar at midnight New York time, in UTC."""
    midnight = datetime.combine(day, time(), tzinfo=EASTERN)
    return midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write(root: Path) -> Path:
    """Write the synthetic snapshots and universe CSV under `root`; return the CSV path."""
    universe_csv = root / "universe" / f"{VERSION}.csv"
    universe_csv.parent.mkdir(parents=True, exist_ok=True)
    lines = ["symbol,name,gics_sector,cik"] + [f"{s},{n},{g},{c}" for s, n, g, c in UNIVERSE]
    universe_csv.write_text("\n".join(lines) + "\n")

    bars = Snapshot(root, source="alpaca-bars", version=VERSION)
    for ticker in [*(row[0] for row in UNIVERSE), BENCHMARK]:
        bars.cover(
            ticker,
            start=f"{START}T04:00:00Z",
            end="2026-10-10T03:59:59Z",
            feed="sip",
            adjustment="raw",
        )
        rows = [
            {"t": _bar_time(day), "o": 100, "h": 102, "l": 99, "c": 101 + n % 3, "v": 1000}
            for n, day in enumerate(SESSIONS)
        ]
        bars.write(
            f"{ticker}/page-0001.json",
            json.dumps({"bars": {ticker: rows}, "next_page_token": None}).encode(),
            url="https://example.invalid/ci-only-synthetic-bars",
            rows=len(rows),
        )

    news = Snapshot(root, source="alpaca-news", version=VERSION)
    edgar = Snapshot(root, source="edgar", version=VERSION)
    for symbol, name, _, cik in UNIVERSE:
        news.cover(symbol, start=f"{START}T00:00:00Z", end=f"{END}T23:59:59Z")
        article = {
            "id": f"synthetic-{symbol}",
            "headline": f"{name} publishes a synthetic update",
            "summary": "CI-only synthetic article.",
            "content": "<p>CI-only synthetic article. Not real news.</p>",
            "symbols": [symbol],
            "created_at": "2026-09-15T13:00:00Z",
            "updated_at": "2026-09-15T13:00:00Z",
        }
        news.write(
            f"{symbol}/page-0001.json",
            json.dumps({"news": [article], "next_page_token": None}).encode(),
            url="https://example.invalid/ci-only-synthetic-news",
            rows=1,
        )
        accession = f"{cik:010d}-26-000001"
        recent = {
            "accessionNumber": [accession],
            "form": ["10-Q"],
            "reportDate": ["2026-06-30"],
            "filingDate": ["2026-08-05"],
            "acceptanceDateTime": ["2026-08-05T20:00:00.000Z"],
            "primaryDocument": ["q2.htm"],
            "items": [""],
        }
        point = {"val": 1, "form": "10-Q", "filed": "2026-08-05", "fy": 2026, "fp": "Q2"}
        revenue = {**point, "accn": accession, "start": "2026-04-01", "end": "2026-06-30"}
        facts = {"cik": cik, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [revenue]}}}}}
        for kind, payload in (
            ("submissions", {"cik": str(cik), "filings": {"recent": recent, "files": []}}),
            ("companyfacts", facts),
        ):
            edgar.write(
                f"{kind}/CIK{cik:010d}.json",
                json.dumps(payload).encode(),
                url="https://example.invalid/ci-only-synthetic-filings",
                rows=1,
            )
        edgar.write(
            f"documents/{cik}/{accession}/q2.htm",
            f"<p>{name} quarterly report. CI-only synthetic filing.</p>".encode(),
            url="https://example.invalid/ci-only-synthetic-document",
            rows=1,
        )
    return universe_csv


if __name__ == "__main__":
    assert os.environ["BAZAAR_SNAPSHOT_VERSION"] == VERSION
    write(Path("/snapshots"))
    print("Created CI-only docker-smoke snapshots and universe (not real market data).")
