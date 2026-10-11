"""The ticker bar: the bundle window's daily closes replayed from the market database, read-only.

The web process shares a container with the market, so it reads BAZAAR_MARKET_DB directly with
a read-only connection for the closes, rather than adding a market route. The window, its
sessions, its symbols and its bars version come from the bundle (bazaar_web.bundle); without a
bundle there are no days. These are historical closes, not live quotes, and they never change,
so the first good read for a bundle is kept.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bazaar_web.bundle import SYMBOLS, Bundle

EASTERN = ZoneInfo("America/New_York")


def session_date(observed_at: str) -> date:
    """The trading date of a stored close instant (stored in UTC)."""
    return datetime.fromisoformat(observed_at).astimezone(EASTERN).date()


def pct_change(close: Decimal, previous: Decimal | None) -> float | None:
    if previous is None or previous <= 0:
        return None
    return float(round((close / previous - 1) * 100, 2))


def read_days(path: Path, bundle: Bundle) -> list[dict[str, Any]]:
    """Each bundle session's closes, with the change from the previous bar (even pre-window)."""
    symbols = bundle.symbols
    sessions = set(bundle.sessions)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    # closing(): a bare "with connect()" commits but never closes, even on success.
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
        marks = ", ".join("?" * len(symbols))
        before = datetime.combine(bundle.end + timedelta(days=1), datetime.min.time(), UTC)
        rows = conn.execute(
            f"SELECT symbol, observed_at, close FROM data_bars WHERE data_version = ? "
            f"AND symbol IN ({marks}) AND observed_at < ? ORDER BY symbol, observed_at",
            (bundle.bars, *symbols, before.isoformat()),
        ).fetchall()

    by_day: dict[date, dict[str, dict[str, Any]]] = {}
    previous: dict[str, Decimal] = {}
    for symbol, observed_at, close_text in rows:
        day, close = session_date(observed_at), Decimal(close_text)
        if day in sessions:
            by_day.setdefault(day, {})[symbol] = {
                "symbol": symbol,
                "close": float(close),
                "change_pct": pct_change(close, previous.get(symbol)),
            }
        previous[symbol] = close
    return [
        {"date": day.isoformat(), "quotes": [q[s] for s in symbols if s in (q := by_day[day])]}
        for day in sorted(by_day)
    ]


class TickerSource:
    def __init__(self, market_db: Path | None) -> None:
        self.market_db = market_db
        self._cached: dict[Bundle, list[dict[str, Any]]] = {}

    def payload(self, bundle: Bundle | None) -> dict[str, Any]:
        """The bundle window's days; no window and no days without a bundle."""
        days = self._days(bundle) if bundle is not None else []
        return {
            "window": {
                "start": bundle.start.isoformat() if bundle else None,
                "end": bundle.end.isoformat() if bundle else None,
                "symbols": list(bundle.symbols if bundle else SYMBOLS),
            },
            "data_version": bundle.bundle_id if bundle and days else None,
            "days": days,
        }

    def _days(self, bundle: Bundle) -> list[dict[str, Any]]:
        if bundle in self._cached:
            return self._cached[bundle]
        if self.market_db is None or not self.market_db.is_file():
            return []
        try:
            days = read_days(self.market_db, bundle)
        except (sqlite3.Error, OSError, ValueError, InvalidOperation):
            return []
        if days:
            self._cached[bundle] = days  # static data: keep the first good read
        return days
