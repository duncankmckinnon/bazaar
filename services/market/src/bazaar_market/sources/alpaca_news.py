"""Alpaca's news archive.

Alpaca serves only the latest revision of each article, and its date filter matches on
`updated_at`. A fetch for a window therefore returns articles last revised inside it, including
ones first published long before, and omits articles revised after the window ended.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx

from .errors import SourceError
from .http import get
from .models import NewsItem
from .snapshot import Snapshot, SnapshotConflict

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"


def parse_news(payload: dict) -> list[NewsItem]:
    return [
        NewsItem(
            source="alpaca",
            id=str(a["id"]),
            symbols=tuple(a["symbols"]),
            headline=a["headline"],
            body=a.get("content") or "",
            created_at=datetime.fromisoformat(a["created_at"]),
            updated_at=datetime.fromisoformat(a["updated_at"]),
        )
        for a in payload["news"]
    ]


def fetch_news(
    http: httpx.Client,
    *,
    symbol: str,
    start: date,
    end: date,
    snap: Snapshot,
    headers: dict[str, str] | None = None,
    until: datetime | None = None,
    min_interval: float = 0.35,
    sleep: Callable[[float], None] = time.sleep,
) -> list[NewsItem]:
    """Freeze every article tagged with `symbol` from the start of `start` to the end of `end`.

    `until`, usually the fetch time, ends the window earlier, so the manifest never claims
    coverage of time that had not happened yet. `headers` carries the Alpaca keys, so they go
    to Alpaca and to no other host. Pages are spaced by `min_interval` seconds because the free
    plan allows 200 requests a minute.
    """
    window_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)
    if until is not None:
        window_end = min(window_end, until.astimezone(UTC).replace(microsecond=0))
    params = {
        "symbols": symbol,
        "start": f"{start.isoformat()}T00:00:00Z",
        "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "include_content": "true",
        "limit": "50",
        "sort": "asc",
    }
    if snap.entry(f"{symbol}/page-0001.json") is not None and snap.coverage(symbol) is None:
        raise SnapshotConflict(
            f"{snap.dir} holds {symbol} news with no recorded window. Fetch into a new version."
        )
    snap.cover(symbol, start=params["start"], end=params["end"])

    items: list[NewsItem] = []
    tokens: set[str] = set()
    page = 0
    requested = False
    while True:
        page += 1
        name = f"{symbol}/page-{page:04d}.json"
        if snap.entry(name) is not None:
            # Frozen by an earlier run of this version. Continue from its page token.
            raw = snap.path(name).read_bytes()
        else:
            if requested:
                sleep(min_interval)
            requested = True
            response = get(http, NEWS_URL, params=params, headers=headers, sleep=sleep)
            if response.status_code != 200:
                raise SourceError(f"Alpaca news returned {response.status_code} for {symbol}")
            raw = response.content
            snap.write(name, raw, url=str(response.url), rows=len(parse_news(json.loads(raw))))
        payload = json.loads(raw)
        items.extend(parse_news(payload))
        token = payload.get("next_page_token")
        if not token:
            return items
        if token in tokens:
            raise SourceError(f"Alpaca news repeated a page token for {symbol} at page {page}")
        tokens.add(token)
        params = {**params, "page_token": token}
