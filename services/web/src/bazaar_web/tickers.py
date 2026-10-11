"""The ticker bar: each replay day's top movers, from the market database, read-only.

The web process shares a container with the market, so it reads BAZAAR_MARKET_DB directly with
a read-only connection for the closes, rather than adding a market route. The window, its
sessions, its symbols and its bars version come from the bundle (bazaar_web.bundle); without a
bundle there are no days. A day shows its TOP_MOVERS universe symbols (never the benchmark) by
the size of their change from the previous close. These are historical closes, not live quotes,
and they never change, so the first good read for a bundle is kept.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bazaar_web.bundle import Bundle

EASTERN = ZoneInfo("America/New_York")
TOP_MOVERS = 20
# Under SQLite's historical limit of 999 bound variables, with room for the other parameters.
CHUNK = 900
# How far before the window to look for each symbol's previous close (covers long holidays).
LOOKBACK = timedelta(days=14)


def session_date(observed_at: str) -> date:
    """The trading date of a stored close instant (stored in UTC)."""
    return datetime.fromisoformat(observed_at).astimezone(EASTERN).date()


def bars_version(conn: sqlite3.Connection, data_version: str) -> str:
    """A bundle id's bars component, or the value itself when it is a plain bars version.

    Mirrors bazaar_market.bundles.component(..., "bars"); a database without bundles has none.
    Only the legacy fallback needs it: the bundle route names its bars version.
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


def read_bars(path: Path, bundle: Bundle, symbols: list[str]) -> list[tuple[str, str, str]]:
    """(symbol, observed_at, close) from LOOKBACK before the window to its end, ordered by
    symbol and time; the symbol list is sent in chunks under SQLite's variable limit."""
    since = datetime.combine(bundle.start - LOOKBACK, datetime.min.time(), UTC)
    before = datetime.combine(bundle.end + timedelta(days=1), datetime.min.time(), UTC)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    rows: list[tuple[str, str, str]] = []
    # closing(): a bare "with connect()" commits but never closes, even on success.
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
        bars = bundle.bars or bars_version(conn, bundle.bundle_id)
        for i in range(0, len(symbols), CHUNK):
            chunk = symbols[i : i + CHUNK]
            rows += conn.execute(
                "SELECT symbol, observed_at, close FROM data_bars WHERE data_version = ? "
                f"AND symbol IN ({', '.join('?' * len(chunk))}) "
                "AND observed_at >= ? AND observed_at < ?",
                (bars, *chunk, since.isoformat(), before.isoformat()),
            ).fetchall()
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def movers(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The TOP_MOVERS quotes by |change_pct|, largest first; ties keep universe order."""
    changed = [q for q in quotes if q["change_pct"] is not None]
    return sorted(changed, key=lambda q: -abs(q["change_pct"]))[:TOP_MOVERS]


def read_days(path: Path, bundle: Bundle) -> list[dict[str, Any]]:
    """Each session's top movers against the previous close (even a pre-window one). Days
    without any change to show are left out."""
    symbols = [s for s in bundle.symbols if s != bundle.benchmark]
    by_day: dict[date, dict[str, dict[str, Any]]] = {}
    previous: dict[str, Decimal] = {}
    for symbol, observed_at, close_text in read_bars(path, bundle, symbols):
        day, close = session_date(observed_at), Decimal(close_text)
        in_window = day in bundle.sessions if bundle.sessions else bundle.start <= day <= bundle.end
        if in_window:
            by_day.setdefault(day, {})[symbol] = {
                "symbol": symbol,
                "close": float(close),
                "change_pct": pct_change(close, previous.get(symbol)),
            }
        previous[symbol] = close
    days = []
    for day in sorted(by_day):
        quotes = movers([q[s] for s in symbols if s in (q := by_day[day])])
        if quotes:
            days.append({"date": day.isoformat(), "quotes": quotes})
    return days


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
                "symbols": list(bundle.symbols) if bundle else None,
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
