"""Read frozen snapshots back into records. No network access.

A company or symbol that is missing from the snapshot is an error, never an empty result,
so missing coverage cannot be mistaken for "nothing happened".
"""

from __future__ import annotations

import json
from pathlib import Path

from .alpaca_news import parse_news
from .edgar import parse_company_facts, parse_submissions
from .errors import SourceError
from .models import Fact, Filing, NewsItem


def load_filings(
    snapshot_dir: Path, cik: int, *, forms: tuple[str, ...] | None = None
) -> list[Filing]:
    folder = Path(snapshot_dir) / "submissions"
    main = folder / f"CIK{cik:010d}.json"
    if not main.exists():
        raise SourceError(f"no filing history for company {cik} in {snapshot_dir}")
    filings = parse_submissions(json.loads(main.read_text()), forms=forms)
    for older in sorted(folder.glob(f"CIK{cik:010d}-submissions-*.json")):
        filings += parse_submissions(json.loads(older.read_text()), cik=cik, forms=forms)
    return sorted(filings, key=lambda f: f.accepted_at)


def load_facts(snapshot_dir: Path, cik: int) -> list[Fact]:
    path = Path(snapshot_dir) / "companyfacts" / f"CIK{cik:010d}.json"
    if not path.exists():
        raise SourceError(f"no reported numbers for company {cik} in {snapshot_dir}")
    return parse_company_facts(json.loads(path.read_text()))


def load_news(snapshot_dir: Path, symbol: str) -> list[NewsItem]:
    folder = Path(snapshot_dir) / symbol
    pages = sorted(folder.glob("page-*.json"))
    if not pages:
        raise SourceError(f"no news for {symbol} in {snapshot_dir}")
    by_id: dict[str, NewsItem] = {}
    for page in pages:
        for item in parse_news(json.loads(page.read_text())):
            by_id.setdefault(item.id, item)
    return sorted(by_id.values(), key=lambda n: (n.created_at, n.id))
