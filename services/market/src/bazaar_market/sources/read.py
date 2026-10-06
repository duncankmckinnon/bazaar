"""Read frozen snapshots back into records. No network access.

A company or symbol that is missing from the snapshot, or whose fetch stopped part way, is an
error and never a shorter result, so missing coverage cannot be mistaken for "nothing happened".
Only files the manifest lists are read, and each must still match its recorded SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .alpaca_news import parse_news
from .edgar import parse_company_facts, parse_submissions
from .errors import SourceError
from .models import Fact, Filing, NewsItem


class _Frozen:
    """The files of one snapshot version that its manifest lists."""

    def __init__(self, snapshot_dir: Path) -> None:
        self.dir = Path(snapshot_dir)
        manifest = self.dir / "manifest.json"
        files = json.loads(manifest.read_text())["files"] if manifest.exists() else []
        self.entries = {e["file"]: e for e in files}

    def has(self, name: str) -> bool:
        if name in self.entries:
            return True
        if (self.dir / name).exists():
            raise SourceError(f"{name} is in {self.dir} but its manifest does not list it")
        return False

    def read(self, name: str) -> dict:
        path = self.dir / name
        if not path.exists():
            raise SourceError(f"{name} is listed in the manifest of {self.dir} but missing")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.entries[name]["sha256"]:
            raise SourceError(f"{name} in {self.dir} does not match its manifest SHA-256")
        return json.loads(content)


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
    return sorted(filings, key=lambda f: f.accepted_at)


def load_facts(snapshot_dir: Path, cik: int) -> list[Fact]:
    frozen = _Frozen(snapshot_dir)
    name = f"companyfacts/CIK{cik:010d}.json"
    if not frozen.has(name):
        raise SourceError(f"no reported numbers for company {cik} in {snapshot_dir}")
    return parse_company_facts(frozen.read(name))


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
