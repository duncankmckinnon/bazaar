"""Market-wide reads at an experiment's cutoff: quotes, the screen, sectors and search.

`as_of_session` is the latest session whose 16:00 New York close is at or before the cutoff, on
the bars version's own calendar (a day with a bar at its close), found by ledger.latest_session
exactly as the fill engine finds it. It can be before the window starts: at the first window
day's open it is the last close before it. available_at does not choose the session; a bar's
values are used only when its available_at is at or before the cutoff, so a bar published late
is missing even after its session closed. The sessions
before as_of_session are the days on which any member has a bar (the ledger's rule: a session is
a day with bars); every one of them closed before the cutoff. pct_change_1d compares the as-of
close with the previous session's, pct_change_5d with the fifth session back. A member without a
usable bar on a session the change needs gets None, never 0.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from bazaar_market import bundles, db
from bazaar_market.ledger import latest_session
from bazaar_market.prices import EASTERN, close_at, session_on

LOOKBACK = timedelta(days=21)  # holds 5 prior sessions with room for holidays
SortKey = Literal["pct_change_1d", "pct_change_5d", "volume"]


class NoUniverse(LookupError):
    """The experiment's data_version is not a bundle with a stored universe."""


class NotRunning(Exception):
    """No session of the bundle's window has closed by the cutoff."""


@dataclass(frozen=True)
class Quote:
    member: bundles.Member
    close: str | None  # as stored
    observed_at: datetime | None
    available_at: datetime | None
    volume: int | None
    change_1d: Decimal | None  # unrounded: close / previous close - 1
    change_5d: Decimal | None


@dataclass(frozen=True)
class View:
    data_version: str
    cutoff_at: datetime
    as_of: date
    quotes: dict[str, Quote]  # every member, the benchmark included, by symbol
    benchmark: str | None

    def constituents(self) -> list[Quote]:
        return [q for q in self.quotes.values() if not q.member.benchmark]


def percent(change: Decimal | None) -> str | None:
    """A change as a percent string with 2 dp, half-even: Decimal("0.02314") -> "2.31"."""
    if change is None:
        return None
    return str((change * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def members(connection: sqlite3.Connection, data_version: str) -> tuple[bundles.Member, ...]:
    found = bundles.universe(connection, data_version)
    if not found:
        raise NoUniverse(f"{data_version} has no stored universe")
    return found


def view(connection: sqlite3.Connection, data_version: str, cutoff: datetime) -> View:
    universe = members(connection, data_version)
    bars_version = bundles.component(connection, data_version, "bars")
    session = latest_session(lambda day: session_on(connection, bars_version, day), cutoff)
    if session is None:
        raise NotRunning(f"no session of {bars_version} has closed by {cutoff.isoformat()}")
    as_of = session.day
    symbols = [m.symbol for m in universe]
    marks = ",".join("?" * len(symbols))
    # observed_at <= the as-of close <= the cutoff, so every row read is of a closed session;
    # rows not yet available still mark their day as a session but their values are not used.
    rows = connection.execute(
        "SELECT symbol, observed_at, available_at, close, volume FROM data_bars "
        f"WHERE data_version = ? AND symbol IN ({marks}) AND observed_at >= ? AND observed_at <= ?",
        (bars_version, *symbols, db.format_time(close_at(as_of - LOOKBACK)),
         db.format_time(close_at(as_of))),
    ).fetchall()  # fmt: skip
    cutoff_text = db.format_time(cutoff)
    days: dict[date, dict[str, sqlite3.Row]] = {}
    for row in rows:
        day = db.parse_time(row["observed_at"]).astimezone(EASTERN).date()
        usable = days.setdefault(day, {})
        if row["available_at"] <= cutoff_text:
            usable[row["symbol"]] = row
    earlier = sorted((d for d in days if d < as_of), reverse=True)
    today = days.get(as_of, {})
    previous = days[earlier[0]] if earlier else {}
    fifth = days[earlier[4]] if len(earlier) >= 5 else {}

    def change(now: sqlite3.Row | None, then: sqlite3.Row | None) -> Decimal | None:
        if now is None or then is None:
            return None
        return Decimal(now["close"]) / Decimal(then["close"]) - 1

    quotes = {}
    for member in universe:
        bar = today.get(member.symbol)
        quotes[member.symbol] = Quote(
            member=member,
            close=bar["close"] if bar else None,
            observed_at=db.parse_time(bar["observed_at"]) if bar else None,
            available_at=db.parse_time(bar["available_at"]) if bar else None,
            volume=bar["volume"] if bar else None,
            change_1d=change(bar, previous.get(member.symbol)),
            change_5d=change(bar, fifth.get(member.symbol)),
        )
    benchmark = next((m.symbol for m in universe if m.benchmark), None)
    return View(data_version, cutoff, as_of, quotes, benchmark)


def _key(quote: Quote, sort: SortKey) -> Decimal | int | None:
    if sort == "volume":
        return quote.volume
    return quote.change_1d if sort == "pct_change_1d" else quote.change_5d


def screen(
    snapshot: View, sort: SortKey, descending: bool, sector: str | None, limit: int
) -> tuple[list[Quote], int]:
    """Constituents (never the benchmark) ordered by `sort` and then symbol ascending, and the
    number left out because their sort key is None. Ordering uses the unrounded value."""
    candidates = [q for q in snapshot.constituents() if sector is None or q.member.sector == sector]
    keyed = [q for q in candidates if _key(q, sort) is not None]
    sign = -1 if descending else 1
    keyed.sort(key=lambda q: (sign * _key(q, sort), q.member.symbol))
    return keyed[:limit], len(candidates) - len(keyed)


@dataclass(frozen=True)
class Sector:
    sector: str
    symbols: int
    with_bars: int
    avg_change_1d: Decimal | None
    avg_change_5d: Decimal | None
    advancers: int
    decliners: int
    unchanged: int


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def sectors(snapshot: View) -> list[Sector]:
    """Equal-weighted per sector over constituents with an as-of bar, by sector name. The
    averages are of unrounded changes, rounded once by `percent`; a constituent whose change is
    None is left out of that average and of the advancer counts."""
    grouped: dict[str, list[Quote]] = {}
    for quote in snapshot.constituents():
        grouped.setdefault(quote.member.sector, []).append(quote)
    result = []
    for name in sorted(grouped):
        quotes = grouped[name]
        ones = [q.change_1d for q in quotes if q.change_1d is not None]
        fives = [q.change_5d for q in quotes if q.change_5d is not None]
        result.append(
            Sector(
                sector=name,
                symbols=len(quotes),
                with_bars=sum(q.close is not None for q in quotes),
                avg_change_1d=_mean(ones),
                avg_change_5d=_mean(fives),
                advancers=sum(c > 0 for c in ones),
                decliners=sum(c < 0 for c in ones),
                unchanged=sum(c == 0 for c in ones),
            )
        )
    return result


def search(universe: tuple[bundles.Member, ...], query: str, limit: int) -> list[bundles.Member]:
    """Case-insensitive. Rank: exact symbol, symbol prefix, name substring, sector substring;
    then symbol ascending. The benchmark is searchable."""
    needle = query.lower()

    def rank(member: bundles.Member) -> int | None:
        symbol = member.symbol.lower()
        if symbol == needle:
            return 0
        if symbol.startswith(needle):
            return 1
        if needle in member.name.lower():
            return 2
        if needle in member.sector.lower():
            return 3
        return None

    ranked = [(r, m.symbol, m) for m in universe if (r := rank(m)) is not None]
    return [m for _, _, m in sorted(ranked)[:limit]]
