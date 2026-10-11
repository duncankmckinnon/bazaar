"""The trading window, from the market's data bundle: GET {market_url}/bundles/{bundle_id}.

The bundle is the one source of the window shown on the board: its dates, its sessions and its
symbols. A good answer is kept for good (a bundle is immutable). After a failure, such as an
unreachable market, a 404 or a malformed body, the next request after RETRY_S tries again, so a
board polled every 2 s makes at most one market call per RETRY_S. While the route gives
nothing, demo-bundle-v1 (main's bundle) falls back to LEGACY_V1_WINDOW, which is that bundle's
window whatever the failure; any other bundle id has no window: no dates and no ticker days.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx
import logfire

SYMBOLS = ("AAPL", "AMZN", "EA", "FISV", "JNJ", "JPM", "KO", "META", "MSFT", "NVDA", "WMT", "XOM")
# Mirrors demo-bundle-v1 for when the market's /bundles route is absent (it is not on main yet):
# exactly main's window today. Remove once that route ships. The bars version comes from the
# market DB, as on main.
LEGACY_V1_BUNDLE_ID = "demo-bundle-v1"
LEGACY_V1_WINDOW = {"start": date(2026, 2, 2), "end": date(2026, 2, 13), "days": 10}
RETRY_S = 30.0
TIMEOUT_S = 2.0

log = logging.getLogger("bazaar_web.bundle")


@dataclass(frozen=True)
class Bundle:
    bundle_id: str
    bars: str | None  # None: resolve the bundle id in the market DB (the legacy fallback)
    start: date
    end: date
    sessions: tuple[date, ...]  # empty: every trading day from start to end (the fallback)
    symbols: tuple[str, ...]
    days: int
    universe_name: str | None = None  # e.g. "S&P 500"; null on demo-bundle-v1
    benchmark: str | None = None  # e.g. "SPY": tradable, but not part of the universe
    # (symbol, sector) for each universe entry; empty when the bundle has no universe field.
    sectors: tuple[tuple[str, str | None], ...] = ()

    @property
    def universe(self) -> str:
        """The header's label: the bundle's display name, else the count of its stocks."""
        if self.universe_name:
            return self.universe_name
        return f"{sum(s != self.benchmark for s in self.symbols)} STOCKS"


def fallback_for(bundle_id: str) -> Bundle | None:
    """LEGACY_V1_WINDOW for demo-bundle-v1, while its route is unavailable; None otherwise."""
    if bundle_id != LEGACY_V1_BUNDLE_ID:
        return None
    return Bundle(
        bundle_id=bundle_id,
        bars=None,
        start=LEGACY_V1_WINDOW["start"],
        end=LEGACY_V1_WINDOW["end"],
        sessions=(),
        symbols=SYMBOLS,
        days=LEGACY_V1_WINDOW["days"],
    )


def universe_symbols(body: dict[str, Any]) -> tuple[str, ...]:
    """The bundle's tickers in display order: its universe [{symbol, name, sector}], else an
    older symbols list, else (demo-bundle-v1) the original 12. ValueError on a bad shape."""
    if body.get("universe") is not None:
        symbols = [u["symbol"] for u in body["universe"]]
    elif body.get("symbols") is not None:
        symbols = body["symbols"]
    else:
        symbols = SYMBOLS
    if not symbols or not all(isinstance(s, str) and s for s in symbols):
        raise ValueError("the universe must be a non-empty list of tickers")
    return tuple(symbols)


def parse(body: Any) -> Bundle:
    """The route's JSON as a Bundle; ValueError (or KeyError/TypeError) on any other shape."""
    start, end = date.fromisoformat(body["start_date"]), date.fromisoformat(body["end_date"])
    sessions = tuple(date.fromisoformat(s) for s in body["sessions"])
    if end < start or any(not start <= s <= end for s in sessions):
        raise ValueError("sessions must fall inside start_date..end_date")
    symbols = universe_symbols(body)
    if not isinstance(body["bars"], str) or not body["bars"]:
        raise ValueError("bars must name a bars version")
    name, benchmark = body.get("universe_name"), body.get("benchmark")
    if not all(v is None or isinstance(v, str) for v in (name, benchmark)):
        raise ValueError("universe_name and benchmark must be strings or null")
    return Bundle(
        bundle_id=str(body["bundle_id"]),
        bars=body["bars"],
        start=start,
        end=end,
        sessions=sessions,
        symbols=symbols,
        days=len(sessions),
        universe_name=(name or "").strip() or None,
        benchmark=(benchmark or "").strip() or None,
        sectors=universe_sectors(body),
    )


def universe_sectors(body: dict[str, Any]) -> tuple[tuple[str, str | None], ...]:
    """(symbol, sector) from the universe [{symbol, name, sector}]; () without a universe."""
    rows = body.get("universe") or ()
    return tuple(
        (u["symbol"], u.get("sector") if isinstance(u.get("sector"), str) else None) for u in rows
    )


class BundleSource:
    def __init__(
        self,
        market_url: str,
        bundle_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = f"{market_url.rstrip('/')}/bundles/{quote(bundle_id, safe='')}"
        self.fallback = fallback_for(bundle_id)
        self.transport = transport
        self.clock = clock
        self._lock = threading.Lock()
        self._bundle: Bundle | None = None
        self._failed_at: float | None = None

    def get(self, *, fallback: bool = True) -> Bundle | None:
        """The route's bundle; while the market cannot give one, the fallback (v1) or None.

        fallback=False gives only a bundle the route answered with (the house lane waits for
        one rather than guess a universe).
        """
        spare = self.fallback if fallback else None
        with self._lock:
            if self._bundle is not None:
                return self._bundle
            if self._failed_at is not None and self.clock() - self._failed_at < RETRY_S:
                return spare
            try:
                self._bundle = self._fetch()
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self._failed_at = self.clock()
                log.warning("no trading window from %s: %s", self.url, type(exc).__name__)
            return self._bundle or spare

    def _fetch(self) -> Bundle:
        # Called from the polled routes, which emit no spans; the cache keeps calls rare anyway.
        with (
            logfire.suppress_instrumentation(),
            httpx.Client(transport=self.transport, timeout=TIMEOUT_S) as client,
        ):
            response = client.get(self.url)
            response.raise_for_status()
            return parse(response.json())
