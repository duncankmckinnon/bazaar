"""The ticker bar: the window's daily closes replayed from the market database, read-only.

The web process shares a container with the market, so it reads BAZAAR_MARKET_DB directly with
a read-only connection rather than adding a market route. These are historical closes for the
fixed window, not live quotes, and they never change, so the first good read is kept.
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bazaar_web.board import SYMBOLS

WINDOW_START = date(2026, 2, 2)
WINDOW_END = date(2026, 2, 13)
EASTERN = ZoneInfo("America/New_York")


def session_date(observed_at: str) -> date:
    """The trading date of a stored close instant (stored in UTC)."""
    return datetime.fromisoformat(observed_at).astimezone(EASTERN).date()


def bars_version(conn: sqlite3.Connection, data_version: str) -> str:
    """A bundle id's bars component, or the value itself when it is a plain bars version.

    Mirrors bazaar_market.bundles.component(..., "bars"); a database without bundles has none.
    """
    try:
        row = conn.execute(
            "SELECT bars FROM data_bundles WHERE bundle_id = ?", (data_version,)
        ).fetchone()
    except sqlite3.OperationalError:
        return data_version
    return row[0] if row else data_version


def pct_change(close: Decimal, previous: Decimal | None) -> float | None:
    if previous is None or previous <= 0:
        return None
    return float(round((close / previous - 1) * 100, 2))


def read_days(path: Path, data_version: str) -> list[dict[str, Any]]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        version = bars_version(conn, data_version)
        marks = ", ".join("?" * len(SYMBOLS))
        before = datetime.combine(WINDOW_END + timedelta(days=1), datetime.min.time(), UTC)
        rows = conn.execute(
            f"SELECT symbol, observed_at, close FROM data_bars WHERE data_version = ? "
            f"AND symbol IN ({marks}) AND observed_at < ? ORDER BY symbol, observed_at",
            (version, *SYMBOLS, before.isoformat()),
        ).fetchall()
    conn.close()

    by_day: dict[date, dict[str, dict[str, Any]]] = {}
    previous: dict[str, Decimal] = {}
    for symbol, observed_at, close_text in rows:
        day, close = session_date(observed_at), Decimal(close_text)
        if WINDOW_START <= day <= WINDOW_END:
            by_day.setdefault(day, {})[symbol] = {
                "symbol": symbol,
                "close": float(close),
                "change_pct": pct_change(close, previous.get(symbol)),
            }
        previous[symbol] = close
    return [
        {"date": day.isoformat(), "quotes": [q[s] for s in SYMBOLS if s in (q := by_day[day])]}
        for day in sorted(by_day)
    ]


class TickerSource:
    def __init__(self, market_db: Path | None, data_version: str) -> None:
        self.market_db = market_db
        self.data_version = data_version
        self._days: list[dict[str, Any]] | None = None

    def payload(self) -> dict[str, Any]:
        if self._days is None and self.market_db is not None and self.market_db.is_file():
            try:
                days = read_days(self.market_db, self.data_version)
            except (sqlite3.Error, OSError, ValueError, InvalidOperation):
                days = []
            if days:
                self._days = days  # static data: keep the first good read
        return {
            "window": {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
                "symbols": SYMBOLS,
            },
            "data_version": self.data_version if self._days else None,
            "days": self._days or [],
        }
