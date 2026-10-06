"""Read frozen snapshots back into records. No network access.

A company or symbol that is missing from the snapshot, or whose fetch stopped part way, is an
error and never a shorter result, so missing coverage cannot be mistaken for "nothing happened".
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
    payload = json.loads(main.read_text())
    filings = parse_submissions(payload, forms=forms)
    for listed in payload["filings"].get("files", []):
        older = folder / Path(listed["name"]).name
        if not older.exists():
            raise SourceError(f"filing history for company {cik} is incomplete: {older.name}")
        filings += parse_submissions(json.loads(older.read_text()), cik=cik, forms=forms)
    return sorted(filings, key=lambda f: f.accepted_at)


def load_facts(snapshot_dir: Path, cik: int) -> list[Fact]:
    path = Path(snapshot_dir) / "companyfacts" / f"CIK{cik:010d}.json"
    if not path.exists():
        raise SourceError(f"no reported numbers for company {cik} in {snapshot_dir}")
    return parse_company_facts(json.loads(path.read_text()))


def load_news(snapshot_dir: Path, symbol: str) -> list[NewsItem]:
    folder = Path(snapshot_dir) / symbol
    if not (folder / "page-0001.json").exists():
        raise SourceError(f"no news for {symbol} in {snapshot_dir}")
    by_id: dict[str, NewsItem] = {}
    number = 0
    while True:
        number += 1
        page = folder / f"page-{number:04d}.json"
        if not page.exists():
            raise SourceError(f"news for {symbol} is incomplete: {page.name} was never frozen")
        payload = json.loads(page.read_text())
        for item in parse_news(payload):
            by_id.setdefault(item.id, item)
        if not payload.get("next_page_token"):
            return sorted(by_id.values(), key=lambda n: (n.created_at, n.id))
