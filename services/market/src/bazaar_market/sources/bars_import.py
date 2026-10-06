"""Import a frozen Alpaca bars snapshot into the market database.

Missing coverage stops the import: a ticker with no bars, a ticker missing a session that other
tickers traded between its first and last bar, or a gap in the demo run's required window.
By default every ticker in the snapshot is checked and imported, all or nothing. An explicit
`symbols` list checks and imports only those tickers, and records the rest as left out.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..prices import Bar, import_bars
from .errors import SourceError
from .read import load_bars, recorded_windows

ALPACA_BARS_VERSION = "alpaca-bars-v1"
DEMO_RUN = (date(2026, 1, 30), date(2026, 2, 13))
DEMO_RUN_SYMBOLS = ("AAPL", "MSFT", "KO")

IMPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_imports (
    data_version TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    source TEXT NOT NULL,
    imported TEXT NOT NULL,
    left_out TEXT NOT NULL,
    PRIMARY KEY (data_version, snapshot)
);
"""


@dataclass(frozen=True)
class TickerCoverage:
    ticker: str
    bars: int
    first: date
    last: date


@dataclass(frozen=True)
class ImportReport:
    coverage: list[TickerCoverage]
    left_out: tuple[str, ...]


def _weekdays(start: date, end: date) -> set[date]:
    days = (start + timedelta(days=n) for n in range((end - start).days + 1))
    return {d for d in days if d.weekday() < 5}


def import_bars_snapshot(
    connection: sqlite3.Connection,
    snapshot_dir: Path,
    *,
    data_version: str = ALPACA_BARS_VERSION,
    required: dict[str, set[date]] | None = None,
    expected: tuple[str, ...] | None = None,
    symbols: tuple[str, ...] | None = None,
) -> ImportReport:
    """Check coverage, then store every bar under `data_version`. Nothing is stored on a failure.

    `expected` lists the tickers the fetch was meant to cover, usually from the config. Each must
    have been fetched, so a fetch that stopped between tickers fails here. Without it, the tickers
    the snapshot recorded are used. `symbols`, when given, replaces `expected`: only those tickers
    are checked and imported, and every other ticker in the snapshot is reported as left out.
    `required` maps a ticker to sessions that must be present. By default it is every weekday
    of the runner's demo run for AAPL, MSFT and KO.
    """
    snapshot_dir = Path(snapshot_dir)
    if required is None:
        required = {s: _weekdays(*DEMO_RUN) for s in DEMO_RUN_SYMBOLS}
    fetched = recorded_windows(snapshot_dir)
    if not fetched:
        raise SourceError(f"{snapshot_dir} records no bar windows")
    feeds = {w["feed"] for w in fetched.values()}
    if len(feeds) != 1 or any(w["adjustment"] != "raw" for w in fetched.values()):
        raise SourceError(f"{snapshot_dir} mixes feeds or holds adjusted bars: {fetched}")

    wanted = symbols if symbols is not None else expected if expected is not None else fetched
    never = sorted(set(wanted) - set(fetched))
    if never:
        raise SourceError(f"missing coverage: {snapshot_dir} never fetched {', '.join(never)}")
    windows = {t: w for t, w in fetched.items() if t in wanted}
    left_out = tuple(sorted(set(fetched) - set(windows)))

    by_ticker: dict[str, list[Bar]] = {}
    for ticker, window in windows.items():
        bars = load_bars(snapshot_dir, ticker)
        if not bars:
            raise SourceError(
                f"missing coverage: Alpaca returned no bars for {ticker} from {window['start']} "
                f"to {window['end']}"
            )
        by_ticker[ticker] = bars

    sessions = {b.session for bars in by_ticker.values() for b in bars}
    problems = []
    for ticker, bars in by_ticker.items():
        held = {b.session for b in bars}
        span = sorted(d for d in sessions if bars[0].session <= d <= bars[-1].session)
        missing = [d for d in span if d not in held]
        if missing:
            problems.append(f"{ticker} has no bar on {', '.join(map(str, missing[:10]))}")
    for ticker, days in required.items():
        held = {b.session for b in by_ticker.get(ticker, [])}
        missing = sorted(days - held)
        if missing:
            problems.append(f"{ticker} is required on {', '.join(map(str, missing))}")
    if problems:
        raise SourceError("missing coverage: " + "; ".join(problems))

    # The snapshot is recorded in data_imports, not in the source, so the same bars from a
    # later fetch import cleanly. A different feed is a different source and needs its own version.
    source = f"alpaca/{feeds.pop()}/raw"
    import_bars(
        connection,
        [b for bars in by_ticker.values() for b in bars],
        data_version=data_version,
        source=source,
    )
    with connection:
        connection.executescript(IMPORTS_SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO data_imports VALUES (?, ?, ?, ?, ?)",
            (
                data_version,
                str(snapshot_dir),
                source,
                ",".join(sorted(by_ticker)),
                ",".join(left_out),
            ),
        )
    coverage = [
        TickerCoverage(ticker, len(bars), bars[0].session, bars[-1].session)
        for ticker, bars in sorted(by_ticker.items())
    ]
    return ImportReport(coverage=coverage, left_out=left_out)
