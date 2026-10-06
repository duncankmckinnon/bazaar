"""Alpaca's news archive.

Alpaca serves only the latest revision of each article, and its date filter matches on
`updated_at`. A fetch for a window therefore returns articles last revised inside it, including
ones first published long before, and omits articles revised after the window ended.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime

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
    min_interval: float = 0.35,
    sleep: Callable[[float], None] = time.sleep,
) -> list[NewsItem]:
    """Freeze every article tagged with `symbol` from the start of `start` to the end of `end`.

    `headers` carries the Alpaca keys, so they go to Alpaca and to no other host. Pages are
    spaced by `min_interval` seconds because the free plan allows 200 requests a minute.
    """
    params = {
        "symbols": symbol,
        "start": f"{start.isoformat()}T00:00:00Z",
        "end": f"{end.isoformat()}T23:59:59Z",
        "include_content": "true",
        "limit": "50",
        "sort": "asc",
    }
    first = snap.entry(f"{symbol}/page-0001.json")
    if first is not None:
        frozen = httpx.URL(first["url"]).params
        if any(frozen.get(key) != params[key] for key in ("symbols", "start", "end")):
            raise SnapshotConflict(
                f"{snap.dir} already holds {symbol} news for {frozen.get('start')} to "
                f"{frozen.get('end')}. Fetch a different window into a new version."
            )

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
