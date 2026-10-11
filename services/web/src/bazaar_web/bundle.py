"""The trading window, from the market's data bundle: GET {market_url}/bundles/{bundle_id}.

The bundle is the one source of the window shown on the board: its dates, its sessions and its
symbols. A good answer is kept for good (a bundle is immutable). After a failure, such as an
unreachable market, a 404 or a malformed body, the next request after RETRY_S tries again, so a
board polled every 2 s makes at most one market call per RETRY_S. Without a bundle there is no
window: the board shows no dates and the ticker no days.
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
RETRY_S = 30.0
TIMEOUT_S = 2.0

log = logging.getLogger("bazaar_web.bundle")


@dataclass(frozen=True)
class Bundle:
    bundle_id: str
    bars: str
    start: date
    end: date
    sessions: tuple[date, ...]
    symbols: tuple[str, ...]


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
    return Bundle(
        bundle_id=str(body["bundle_id"]),
        bars=body["bars"],
        start=start,
        end=end,
        sessions=sessions,
        symbols=symbols,
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
        self.transport = transport
        self.clock = clock
        self._lock = threading.Lock()
        self._bundle: Bundle | None = None
        self._failed_at: float | None = None

    def get(self) -> Bundle | None:
        """The bundle, or None while the market cannot give one."""
        with self._lock:
            if self._bundle is not None:
                return self._bundle
            if self._failed_at is not None and self.clock() - self._failed_at < RETRY_S:
                return None
            try:
                self._bundle = self._fetch()
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self._failed_at = self.clock()
                log.warning("no trading window from %s: %s", self.url, type(exc).__name__)
            return self._bundle

    def _fetch(self) -> Bundle:
        # Called from the polled routes, which emit no spans; the cache keeps calls rare anyway.
        with (
            logfire.suppress_instrumentation(),
            httpx.Client(transport=self.transport, timeout=TIMEOUT_S) as client,
        ):
            response = client.get(self.url)
            response.raise_for_status()
            return parse(response.json())
