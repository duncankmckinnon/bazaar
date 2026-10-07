"""Alpaca's daily stock bars, unadjusted.

Bars are requested with `adjustment=raw`. Split- or dividend-adjusted history rewrites old prices
with corporate actions that happened later, which would leak the future into the past.
Each ticker is requested with `asof=-`, which turns off Alpaca's mapping of a renamed company's
old ticker onto its new one, so a request returns only the bars traded under that ticker.
A daily bar's `t` is midnight Eastern on its session date.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from ..prices import EASTERN, RENAMED_FROM, Bar
from .errors import SourceError
from .http import get
from .snapshot import Snapshot, SnapshotConflict

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
ADJUSTMENT = "raw"


@dataclass(frozen=True)
class TickerWindow:
    """One ticker over the sessions it traded under that name, inclusive."""

    ticker: str
    start: date
    end: date


def ticker_windows(tickers: tuple[str, ...], start: date, end: date) -> list[TickerWindow]:
    """Split each company's period at a rename, so every request uses the ticker of its day."""
    windows = []
    for ticker in tickers:
        old, renamed_on = RENAMED_FROM.get(ticker, (ticker, start))
        if start < renamed_on <= end:
            windows.append(TickerWindow(old, start, renamed_on - timedelta(days=1)))
            windows.append(TickerWindow(ticker, renamed_on, end))
        else:
            windows.append(TickerWindow(ticker, start, end))
    return windows


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_bars(payload: dict, ticker: str) -> list[Bar]:
    return [
        Bar(
            symbol=ticker,
            session=datetime.fromisoformat(b["t"]).astimezone(EASTERN).date(),
            open=Decimal(str(b["o"])),
            high=Decimal(str(b["h"])),
            low=Decimal(str(b["l"])),
            close=Decimal(str(b["c"])),
            volume=int(b["v"]),
        )
        for b in (payload.get("bars") or {}).get(ticker, [])
    ]


def load_page(raw: bytes) -> dict:
    """Prices are read as exact decimals, never as binary floats."""
    return json.loads(raw, parse_float=Decimal)


def fetch_bars(
    http: httpx.Client,
    *,
    window: TickerWindow,
    snap: Snapshot,
    feed: str,
    headers: dict[str, str] | None = None,
    min_interval: float = 0.35,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Bar]:
    """Freeze every daily bar for one ticker window. Returns the bars, which may be none.

    The manifest records the window, feed and adjustment. An interrupted fetch resumes from the
    pages already frozen when rerun into the same version.
    """
    start = datetime.combine(window.start, datetime.min.time(), tzinfo=EASTERN)
    end = datetime.combine(window.end, datetime.max.time(), tzinfo=EASTERN)
    params = {
        "symbols": window.ticker,
        "timeframe": "1Day",
        "start": _rfc3339(start),
        "end": _rfc3339(end.replace(microsecond=0)),
        "adjustment": ADJUSTMENT,
        "feed": feed,
        "asof": "-",
        "limit": "10000",
        "sort": "asc",
    }
    key = window.ticker
    if snap.entry(f"{key}/page-0001.json") is not None and snap.coverage(key) is None:
        raise SnapshotConflict(f"{snap.dir} holds {key} bars with no recorded window.")
    snap.cover(key, start=params["start"], end=params["end"], adjustment=ADJUSTMENT, feed=feed)

    bars: list[Bar] = []
    tokens: set[str] = set()
    page = 0
    requested = False
    while True:
        page += 1
        name = f"{key}/page-{page:04d}.json"
        if snap.entry(name) is not None:
            raw = snap.path(name).read_bytes()
        else:
            if requested:
                sleep(min_interval)
            requested = True
            response = get(http, BARS_URL, params=params, headers=headers, sleep=sleep)
            if response.status_code != 200:
                raise SourceError(
                    f"Alpaca bars returned {response.status_code} for {key}: {response.text[:200]}"
                )
            raw = response.content
            rows = len(parse_bars(load_page(raw), key))
            snap.write(name, raw, url=str(response.url), rows=rows)
        payload = load_page(raw)
        bars.extend(parse_bars(payload, key))
        token = payload.get("next_page_token")
        if not token:
            return bars
        if token in tokens:
            raise SourceError(f"Alpaca bars repeated a page token for {key} at page {page}")
        tokens.add(token)
        params = {**params, "page_token": token}
