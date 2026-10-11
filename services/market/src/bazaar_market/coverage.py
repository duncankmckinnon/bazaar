"""Coverage of a bundle's window in a market database: bars, news and filings per symbol.

    uv run python -m bazaar_market.coverage --db data/market.sqlite3 --bundle demo-bundle-v2

It opens the database read-only. The window, holidays and symbols default to the bundle's own:
its stored universe and benchmark. It prints a line per symbol, then a summary. It exits 1 when
anything the demo needs is missing:
- any expected session (a weekday in the window that is not a stored holiday) lacks a bar for
  any universe symbol or the benchmark, including a whole day with no bars;
- the bundle has no stored universe;
- news or filings were never imported for the bundle's versions.
News and filings are best effort per symbol: a symbol whose news coverage does not span the
window, or with no fiscal period end visible by the window's last close, is reported and counted
in the summary but does not fail the check. --allow-missing-news and --allow-missing-filings
accept a seed with no news or no filings at all.
The summary's ticker changes are the renames the bars import recorded inside the window, the
symbols the universe import excluded, and bars that start or stop inside the window.
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

from bazaar_market import bundles, db
from bazaar_market.archive import MissingCoverage, stored_time
from bazaar_market.filings import latest_period_ends
from bazaar_market.prices import EASTERN, close_at


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows}


def resolve(
    connection: sqlite3.Connection, bundle_id: str
) -> tuple[dict[str, str], bundles.Window, tuple[bundles.Member, ...]]:
    """The bundle's components, window and universe, from the database if seeded, else from the
    code (where only demo-bundle-v1 has a universe)."""
    tables = _tables(connection)
    if {"data_bundles", "data_bundle_windows"} <= tables:
        window = bundles.window(connection, bundle_id)
        if window is not None:
            kinds = ("bars", "news", "filings")
            members = bundles.universe(connection, bundle_id) if "data_universe" in tables else ()
            parts = {k: bundles.component(connection, bundle_id, k) for k in kinds}
            return parts, window, members
    if bundle_id not in bundles.DEMO_BUNDLES or bundle_id not in bundles.DEMO_WINDOWS:
        raise SystemExit(f"unknown bundle {bundle_id}")
    return (
        dict(bundles.DEMO_BUNDLES[bundle_id]),
        bundles.DEMO_WINDOWS[bundle_id],
        bundles.DEMO_UNIVERSES.get(bundle_id, ()),
    )


def recorded_changes(
    connection: sqlite3.Connection,
    tables: set[str],
    bundle_id: str,
    bars_version: str,
    window: bundles.Window,
) -> list[str]:
    """Renames the bars import applied inside the window, and the symbols the universe import
    left out, as recorded by those imports (data_ticker_changes, data_universe_exclusions). A
    missing table is reported as not recorded, so a database seeded before them cannot look
    clean."""
    changes = []
    if "data_ticker_changes" in tables:
        rows = connection.execute(
            "SELECT symbol, ticker, effective FROM data_ticker_changes "
            "WHERE data_version = ? AND effective >= ? AND effective <= ? ORDER BY effective, symbol",
            (bars_version, window.start.isoformat(), window.end.isoformat()),
        )
        changes += [f"{s} <- {t} from {e} (recorded at import)" for s, t, e in rows]
    else:
        changes.append("renames not recorded (no data_ticker_changes table)")
    if "data_universe_exclusions" in tables:
        rows = connection.execute(
            "SELECT symbol, reason, last_bar FROM data_universe_exclusions WHERE bundle_id = ? "
            "ORDER BY symbol",
            (bundle_id,),
        )
        changes += [
            f"{s} excluded from the universe: {r}" + (f", last bar {b}" if b else "")
            for s, r, b in rows
        ]
    else:
        changes.append("exclusions not recorded (no data_universe_exclusions table)")
    return changes


def report(
    db_path: Path,
    bundle_id: str,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    *,
    allow_missing_news: bool = False,
    allow_missing_filings: bool = False,
) -> tuple[list[str], list[str]]:
    """The report's lines, and its failures (empty when the window is fully covered)."""
    with closing(db.connect(db_path, read_only=True)) as connection:
        parts, stored, members = resolve(connection, bundle_id)
        window = bundles.Window(start or stored.start, end or stored.end, stored.holidays)
        if symbols:
            checked = sorted(set(symbols))
        else:
            checked = sorted(m.symbol for m in members)
        opened = datetime.combine(window.start, time(0), tzinfo=EASTERN)
        cutoff = close_at(window.end)
        tables = _tables(connection)
        benchmark = f", benchmark {stored.benchmark}" if stored.benchmark else ""
        lines = [
            f"bundle {bundle_id}: bars {parts['bars']}, news {parts['news']}, "
            + f"filings {parts['filings']}",
            f"window {window.start}..{window.end}, holidays "
            + f"{', '.join(map(str, window.holidays)) or 'none'}, {len(checked)} symbols"
            + benchmark,
            f"news must cover {stored_time(opened)}..{stored_time(cutoff)}; filings visible by "
            + stored_time(cutoff),
        ]
        failures: list[str] = []
        if allow_missing_news:
            lines.append("WARNING: --allow-missing-news: missing news does NOT fail this check")
        if allow_missing_filings:
            lines.append("WARNING: --allow-missing-filings: missing filings do NOT fail this check")
        if not checked:
            return [*lines, "no universe stored"], [f"{bundle_id} has no stored universe"]
        if "data_bars" not in tables:
            return [*lines, "no bars imported"], ["no bars imported"]

        found = bundles.sessions(connection, parts["bars"], window, checked)
        lines.append(f"expected sessions {len(found.expected)}, complete {len(found.complete)}")
        for day, missing in found.incomplete:
            if len(missing) == len(checked):
                failures.append(f"{day}: no bars for any symbol (whole day missing)")

        news_imported = {"data_news", "data_news_coverage"} <= tables and connection.execute(
            "SELECT 1 FROM data_news_coverage WHERE data_version = ? LIMIT 1", (parts["news"],)
        ).fetchone() is not None
        if not news_imported:
            (lines if allow_missing_news else failures).append(
                f"news {parts['news']} was never imported"
            )

        ends: dict[str, date] = {}
        filings_imported = (
            "data_filings" in tables
            and connection.execute(
                "SELECT 1 FROM data_filings WHERE data_version = ? LIMIT 1", (parts["filings"],)
            ).fetchone()
            is not None
        )
        if filings_imported:
            try:
                ends = latest_period_ends(connection, parts["filings"], checked, cutoff)
            except MissingCoverage:
                filings_imported = False
        if not filings_imported:
            (lines if allow_missing_filings else failures).append(
                f"filings {parts['filings']} were never imported"
            )

        full_bars, news_spans, filings_cycles = 0, 0, 0
        changes = recorded_changes(connection, tables, bundle_id, parts["bars"], window)
        for symbol in checked:
            days = found.bars[symbol]
            missing = [d for d in found.expected if d not in days]
            row = f"{symbol:<6} bars {len(found.expected) - len(missing)}/{len(found.expected)}"
            if missing:
                row += f" MISSING {', '.join(map(str, missing))}"
                failures.append(f"{symbol}: no bar on {', '.join(map(str, missing))}")
            else:
                full_bars += 1
            if days and found.expected and days[0] > found.expected[0]:
                changes.append(f"{symbol} bars start {days[0]}")
            if days and found.expected and days[-1] < found.expected[-1]:
                changes.append(f"{symbol} bars stop after {days[-1]}")
            if news_imported:
                (count,) = connection.execute(
                    "SELECT COUNT(*) FROM data_news WHERE data_version = ? AND symbol = ? "
                    "AND available_at >= ? AND available_at <= ?",
                    (parts["news"], symbol, stored_time(opened), stored_time(cutoff)),
                ).fetchone()
                covered = connection.execute(
                    "SELECT start_at, end_at FROM data_news_coverage "
                    "WHERE data_version = ? AND symbol = ?",
                    (parts["news"], symbol),
                ).fetchone()
                spans = covered is not None and (
                    covered[0] <= stored_time(opened) and stored_time(cutoff) <= covered[1]
                )
                span = f"{covered[0][:10]}..{covered[1][:10]}" if covered else "no coverage"
                row += f" | news {count} (coverage {span})"
                if spans:
                    news_spans += 1
                else:
                    row += " NEWS DOES NOT COVER THE WINDOW"
            if filings_imported:
                (visible,) = connection.execute(
                    "SELECT COUNT(*) FROM data_filings WHERE data_version = ? AND symbol = ? "
                    "AND accepted_at <= ?",
                    (parts["filings"], symbol, stored_time(cutoff)),
                ).fetchone()
                latest = ends.get(symbol)
                if latest:
                    filings_cycles += 1
                    cycle = f"latest period end {latest}, cycle start {latest + timedelta(days=1)}"
                else:
                    cycle = "NO FISCAL PERIOD END VISIBLE"
                row += f" | filings {visible} visible, {cycle}"
            lines.append(row)

        n = len(checked)
        news_line = f"{news_spans}/{n}" if news_imported else "never imported"
        filings_line = f"{filings_cycles}/{n}" if filings_imported else "never imported"
        lines += [
            "summary:",
            f"  bars: {full_bars}/{n} symbols full, {n - full_bars} with missing bars",
            f"  news coverage spanning the window: {news_line}",
            f"  filings with a fiscal period end by the window end: {filings_line}",
            "  ticker changes in the window: " + ("; ".join(changes) or "none"),
        ]
    return lines, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bazaar_market.coverage", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--symbols", help="comma-separated; defaults to the bundle's universe")
    parser.add_argument("--allow-missing-news", action="store_true")
    parser.add_argument("--allow-missing-filings", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.exists():
        parser.error(f"{args.db} does not exist")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    lines, failures = report(
        args.db, args.bundle, args.start, args.end, symbols,
        allow_missing_news=args.allow_missing_news,
        allow_missing_filings=args.allow_missing_filings,
    )  # fmt: skip
    print("\n".join(lines))
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
