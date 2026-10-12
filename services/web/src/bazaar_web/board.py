"""The /api/board payload: scored runs from the runs directory plus in-flight submissions."""

import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bazaar_replay.leaderboard import Entry, Leaderboard, Run, load_run
from bazaar_replay.leaderboard import build_board as rank_runs

from bazaar_web.bundle import Bundle
from bazaar_web.store import Store

DEFAULT_STARTING_CASH = Decimal(10000)
STATUS_ORDER = {"scored": 0, "running": 1, "queued": 2, "failed": 3}
# /api/board sends the top TOP_ROWS scored rows (plus every running, queued and baseline row);
# ranks are always over every row. Histories go only to what the chart draws.
TOP_ROWS = 100
CHART_TOP = 10


def percent(fraction: Decimal | None) -> float | None:
    """Evaluator fractions become percent exactly once, here: 0.006011 -> 0.6."""
    if fraction is None:
        return None
    return float(round(fraction * 100, 2))


def money(value: Decimal) -> float:
    return float(round(value, 2))


def points(values: list[tuple[int, Decimal]]) -> list[dict[str, Any]] | None:
    """Chart points: portfolio value in dollars after each trading day."""
    return [{"day": day, "value": money(value)} for day, value in values] or None


def history(run_dir: Path) -> list[dict[str, Any]] | None:
    """Portfolio value after each session close (day 1..n), from the run's record.json marks."""
    try:
        record = json.loads((run_dir / "record.json").read_text())
        values = [Decimal(mark["snapshot"]["portfolio_value"]) for mark in record["marks"]]
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        return None
    return points(list(enumerate(values, start=1)))


def live_history(progress: list[tuple[int, str]]) -> list[dict[str, Any]] | None:
    try:
        return points([(day, Decimal(value)) for day, value in progress])
    except InvalidOperation:
        return None


# The run dir's inode and mtime_ns, then record.json's and evaluation.json's mtime_ns (-1 if
# missing): an operator rewriting a baseline's files in place refreshes it.
RunKey = tuple[int, int, int, int]
Histories = dict[str, list[dict[str, Any]] | None]


def run_key(path: Path) -> RunKey:
    """OSError if the dir itself is gone."""
    stat = path.stat()
    files = []
    for name in ("record.json", "evaluation.json"):
        try:
            files.append((path / name).stat().st_mtime_ns)
        except OSError:
            files.append(-1)
    return (stat.st_ino, stat.st_mtime_ns, *files)


class BoardSource:
    """The leaderboard over the runs directory, rebuilt at most every ttl seconds (the board
    polls every 2 s).

    Published run dirs are immutable (the runner stages them and renames them into place), so
    each one is parsed once: its load_run result and mark history are kept by dir name, guarded
    by run_key. A rebuild lists the directory, parses only new or changed dirs, drops removed
    ones, and ranks all of them again (cheap once parsed). Dirs of hidden submissions are
    never parsed (most of production's are hidden duplicates); a change to the hidden set
    rebuilds at once, so an un-hidden row is back on the next poll.
    """

    def __init__(self, runs_dir: Path, ttl: float = 2.0) -> None:
        self.runs_dir = runs_dir
        self.ttl = ttl
        self._lock = threading.Lock()
        self._runs: dict[str, tuple[RunKey, Run | Entry, list[dict[str, Any]] | None]] = {}
        self._cached: tuple[float, frozenset[str], Leaderboard, Histories] | None = None

    def invalidate(self) -> None:
        self._cached = None

    def load(self, hidden: frozenset[str] = frozenset()) -> tuple[Leaderboard, Histories]:
        """The board without the run dirs named in hidden."""
        with self._lock:  # sync routes run in a threadpool: one rebuild at a time
            now = time.monotonic()
            cached = self._cached
            if cached is None or now - cached[0] >= self.ttl or cached[1] != hidden:
                self._cached = cached = (now, hidden, *self._rebuild(hidden))
            return cached[2], cached[3]

    def _rebuild(self, hidden: frozenset[str]) -> tuple[Leaderboard, Histories]:
        if not self.runs_dir.is_dir():
            empty = Leaderboard(
                header=None, reference_run_id=None, ranked=(), failed=(), not_comparable=(),
                invalid=(),
            )  # fmt: skip
            return empty, {}
        # Dot-prefixed dirs (".tmp-<uuid>") are runs still being staged by the runner.
        dirs = sorted(
            p
            for p in self.runs_dir.iterdir()
            if not p.name.startswith(".") and p.name not in hidden and p.is_dir()
        )
        runs: dict[str, tuple[RunKey, Run | Entry, list[dict[str, Any]] | None]] = {}
        for path in dirs:
            try:
                key = run_key(path)
            except OSError:
                continue  # removed between the listing and now
            cached = self._runs.get(path.name)
            if cached is None or cached[0] != key:
                cached = (key, load_run(path), history(path))
            runs[path.name] = cached
        self._runs = runs  # removed dirs drop out here
        board = rank_runs([loaded for _, loaded, _ in runs.values()])
        histories = {e.run_id: runs[e.run_id][2] for e in board.ranked if e.run_id in runs}
        return board, histories


def entry_row(
    entry: Entry, status: str, submission: dict[str, Any] | None, hist: list[float] | None
) -> dict[str, Any]:
    scored = status == "scored"
    return {
        "id": submission["id"] if submission else entry.run_id,
        "name": submission["name"] if submission else entry.policy_ref or entry.run_id,
        "handle": submission["handle"] if submission else None,
        "lane": lane_of(submission),
        "generation": generation_of(submission),
        "kind": entry.kind or "agent",
        "status": status,
        "day": submission["day"] if submission else None,
        "return_pct": percent(entry.period_return) if scored else None,
        "excess_pct": percent(entry.excess_vs_buy_and_hold) if scored else None,
        "fills": entry.orders_filled if scored else None,
        "history": hist if scored else None,
        "trace_id": entry.trace_id,
        "provisional": False,
    }


def lane_of(submission: dict[str, Any] | None) -> str | None:
    """'house' or 'human' for a submission's row; None for runs not submitted here (baselines)."""
    if submission is None:
        return None
    return submission.get("lane") or "human"


def generation_of(submission: dict[str, Any] | None) -> int | None:
    """The strategy factory's generation for a house row; None for every other row."""
    if submission is None or lane_of(submission) != "house":
        return None
    return submission.get("generation")


def provisional_return(submission: dict[str, Any], starting_cash: Decimal) -> float | None:
    """A running run's return so far, from the last marked value it reported."""
    value = submission.get("latest_value")
    if submission["status"] != "running" or value is None or starting_cash <= 0:
        return None
    try:
        return percent(Decimal(value) / starting_cash - 1)
    except InvalidOperation:
        return None


def submission_row(
    submission: dict[str, Any],
    starting_cash: Decimal,
    progress: list[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    live = provisional_return(submission, starting_cash)
    running = submission["status"] == "running"
    return {
        "id": submission["id"],
        "name": submission["name"],
        "handle": submission["handle"],
        "lane": lane_of(submission),
        "generation": generation_of(submission),
        "kind": "agent",
        "status": submission["status"],
        "day": submission["day"],
        "return_pct": live,
        "excess_pct": None,
        "fills": None,
        "history": live_history(progress) if running and progress else None,
        "trace_id": None,
        "provisional": live is not None,
    }


def board_rows(
    source: BoardSource,
    store: Store,
    logfire_url: Callable[[str], str | None] = lambda _: None,
) -> tuple[list[dict[str, Any]], Decimal]:
    """Every row, ranked over all of them, with histories; and the starting cash."""
    by_dir = store.by_run_dir()
    board, histories = source.load(frozenset(d for d, s in by_dir.items() if s["hidden"]))
    rows = []
    for status, entries in (("scored", board.ranked), ("failed", board.failed)):
        for entry in entries:
            submission = by_dir.get(entry.run_id)
            if submission and submission["hidden"]:
                continue
            rows.append(entry_row(entry, status, submission, histories.get(entry.run_id)))
    starting_cash = board.header.starting_cash if board.header else DEFAULT_STARTING_CASH
    in_flight = store.in_flight()
    progress = store.progress([s["id"] for s in in_flight if s["status"] == "running"])
    rows += [submission_row(s, starting_cash, progress.get(s["id"])) for s in in_flight]

    # Stable sorts: scored by return (null last), then running, queued (FIFO) and failed.
    rows.sort(key=lambda r: -r["return_pct"] if r["status"] == "scored" and r["return_pct"] else 0)
    rows.sort(key=lambda r: r["status"] == "scored" and r["return_pct"] is None)
    rows.sort(key=lambda r: STATUS_ORDER[r["status"]])
    scored = 0
    for row in rows:
        row["logfire_url"] = logfire_url(row["name"])
        if row["status"] == "scored":
            scored += 1
            row["rank"] = scored
        else:
            row["rank"] = None
    return rows, starting_cash


def page_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the page draws: the top TOP_ROWS scored, every running, queued and baseline row
    (failed runs are not on the board). History only for the chart: the top CHART_TOP scored,
    the baselines and the running rows."""
    shown = []
    for row in rows:
        baseline = row["kind"] == "baseline"
        top = row["status"] == "scored" and row["rank"] is not None and row["rank"] <= TOP_ROWS
        if not (top or baseline or row["status"] in ("running", "queued")):
            continue
        charted = row["status"] == "running" or (
            row["status"] == "scored" and (baseline or row["rank"] <= CHART_TOP)
        )
        shown.append(row if charted or row["history"] is None else {**row, "history": None})
    return shown


def build_board(
    source: BoardSource,
    store: Store,
    logfire_url: Callable[[str], str | None] = lambda _: None,
    bundle: Bundle | None = None,
) -> dict[str, Any]:
    rows, starting_cash = board_rows(source, store, logfire_url)

    # The window and universe are the data bundle's; without one the board shows neither.
    window = {
        "start": bundle.start.isoformat() if bundle else None,
        "end": bundle.end.isoformat() if bundle else None,
        "starting_cash": float(starting_cash),
        "universe": bundle.universe if bundle else None,
        "symbols": list(bundle.symbols) if bundle else None,
        "days": bundle.days if bundle else None,
    }
    return {
        "window": window,
        "rows": page_rows(rows),
        "total": {"rows": len(rows), **Counter(row["status"] for row in rows)},
        "events": store.recent_events(20),
    }
