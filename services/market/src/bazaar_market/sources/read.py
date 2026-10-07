"""Read frozen snapshots back into records. No network access.

A company or symbol that is missing from the snapshot, or whose fetch stopped part way, is an
error and never a shorter result, so missing coverage cannot be mistaken for "nothing happened".
Only files the manifest lists are read, and each must still match its recorded SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..prices import Bar
from .alpaca_bars import load_page, parse_bars
from .alpaca_news import parse_news
from .edgar import check_acceptance_label, parse_company_facts, parse_submissions
from .errors import SourceError
from .models import Fact, Filing, NewsItem


class _Frozen:
    """The files of one snapshot version that its manifest lists."""

    def __init__(self, snapshot_dir: Path) -> None:
        self.dir = Path(snapshot_dir)
        manifest = self.dir / "manifest.json"
        payload = json.loads(manifest.read_text()) if manifest.exists() else {}
        self.entries = {e["file"]: e for e in payload.get("files", [])}
        self.coverage = payload.get("coverage", {})

    def has(self, name: str) -> bool:
        if name in self.entries:
            return True
        if (self.dir / name).exists():
            raise SourceError(f"{name} is in {self.dir} but its manifest does not list it")
        return False

    def read(self, name: str) -> dict:
        return json.loads(self.read_bytes(name))

    def read_bytes(self, name: str) -> bytes:
        path = self.dir / name
        if not path.exists():
            raise SourceError(f"{name} is listed in the manifest of {self.dir} but missing")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.entries[name]["sha256"]:
            raise SourceError(f"{name} in {self.dir} does not match its manifest SHA-256")
        return content


def load_filings(
    snapshot_dir: Path, cik: int, *, forms: tuple[str, ...] | None = None
) -> list[Filing]:
    frozen = _Frozen(snapshot_dir)
    main = f"submissions/CIK{cik:010d}.json"
    if not frozen.has(main):
        raise SourceError(f"no filing history for company {cik} in {snapshot_dir}")
    payload = frozen.read(main)
    filings = parse_submissions(payload, forms=forms)
    for listed in payload["filings"].get("files", []):
        older = f"submissions/{Path(listed['name']).name}"
        if not frozen.has(older):
            raise SourceError(f"filing history for company {cik} is incomplete: {older}")
        filings += parse_submissions(frozen.read(older), cik=cik, forms=forms)
    for filing in filings:
        check_acceptance_label(filing)
    return sorted(filings, key=lambda f: f.accepted_at)


def load_facts(snapshot_dir: Path, cik: int) -> list[Fact]:
    frozen = _Frozen(snapshot_dir)
    name = f"companyfacts/CIK{cik:010d}.json"
    if not frozen.has(name):
        raise SourceError(f"no reported numbers for company {cik} in {snapshot_dir}")
    return parse_company_facts(frozen.read(name))


def news_coverage(snapshot_dir: Path, symbol: str) -> tuple[datetime, datetime]:
    """The window a symbol's news was fetched for. Outside it, no news means not fetched."""
    window = _Frozen(snapshot_dir).coverage.get(symbol)
    if window is None:
        raise SourceError(f"no news window recorded for {symbol} in {snapshot_dir}")
    return datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])


def load_news(snapshot_dir: Path, symbol: str) -> list[NewsItem]:
    frozen = _Frozen(snapshot_dir)
    if not frozen.has(f"{symbol}/page-0001.json"):
        raise SourceError(f"no news for {symbol} in {snapshot_dir}")
    by_id: dict[str, NewsItem] = {}
    number = 0
    while True:
        number += 1
        page = f"{symbol}/page-{number:04d}.json"
        if not frozen.has(page):
            raise SourceError(f"news for {symbol} is incomplete: {page} was never frozen")
        payload = frozen.read(page)
        for item in parse_news(payload):
            by_id.setdefault(item.id, item)
        if not payload.get("next_page_token"):
            return sorted(by_id.values(), key=lambda n: (n.created_at, n.id))


def recorded_windows(snapshot_dir: Path) -> dict[str, dict[str, str]]:
    """Each symbol's recorded request window, with any details such as adjustment and feed."""
    return dict(_Frozen(snapshot_dir).coverage)


def load_bars(snapshot_dir: Path, ticker: str) -> list[Bar]:
    """Every frozen daily bar for one ticker. A fetch that stopped part way raises."""
    frozen = _Frozen(snapshot_dir)
    if ticker not in frozen.coverage or not frozen.has(f"{ticker}/page-0001.json"):
        raise SourceError(f"no bars for {ticker} in {snapshot_dir}")
    bars: list[Bar] = []
    number = 0
    while True:
        number += 1
        page = f"{ticker}/page-{number:04d}.json"
        if not frozen.has(page):
            raise SourceError(f"bars for {ticker} are incomplete: {page} was never frozen")
        payload = load_page(frozen.read_bytes(page))
        bars += parse_bars(payload, ticker)
        if not payload.get("next_page_token"):
            return sorted(bars, key=lambda b: b.session)


def load_document(snapshot_dir: Path, filing: Filing) -> bytes | None:
    """A filing's frozen primary document, checked against the manifest. None if not frozen."""
    if not filing.primary_document:
        return None
    frozen = _Frozen(snapshot_dir)
    name = f"documents/{filing.cik}/{filing.accession}/{filing.primary_document}"
    return frozen.read_bytes(name) if frozen.has(name) else None
