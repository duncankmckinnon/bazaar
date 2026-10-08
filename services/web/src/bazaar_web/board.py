"""The /api/board payload: scored runs from the runs directory plus in-flight submissions."""

import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bazaar_replay.leaderboard import Entry, Leaderboard, load_board

from bazaar_web.store import Store

SYMBOLS = ["AAPL", "MSFT", "KO"]
DAYS = 10
DEFAULT_WINDOW = {"start": "2026-02-02", "end": "2026-02-13", "starting_cash": 10000.0}
STATUS_ORDER = {"scored": 0, "running": 1, "queued": 2, "failed": 3}


def percent(fraction: Decimal | None) -> float | None:
    """Evaluator fractions become percent exactly once, here: 0.006011 -> 0.6."""
    if fraction is None:
        return None
    return float(round(fraction * 100, 2))


def history(run_dir: Path) -> list[float] | None:
    """Cumulative return in percent at each mark, from the run's record.json."""
    try:
        record = json.loads((run_dir / "record.json").read_text())
        cash = Decimal(record["manifest"]["starting_cash"])
        values = [Decimal(mark["snapshot"]["portfolio_value"]) for mark in record["marks"]]
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        return None
    if not values or cash <= 0:
        return None
    return [percent(value / cash - 1) for value in values]


class BoardSource:
    """Caches load_board and the mark histories for a short time; the board polls every 2s."""

    def __init__(self, runs_dir: Path, ttl: float = 2.0) -> None:
        self.runs_dir = runs_dir
        self.ttl = ttl
        self._cached: tuple[float, Leaderboard, dict[str, list[float] | None]] | None = None

    def invalidate(self) -> None:
        self._cached = None

    def load(self) -> tuple[Leaderboard, dict[str, list[float] | None]]:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] >= self.ttl:
            if self.runs_dir.is_dir():
                board = load_board(self.runs_dir)
            else:
                board = Leaderboard(
                    header=None,
                    reference_run_id=None,
                    ranked=(),
                    failed=(),
                    not_comparable=(),
                    invalid=(),
                )
            histories = {e.run_id: history(self.runs_dir / e.run_id) for e in board.ranked}
            self._cached = (now, board, histories)
        return self._cached[1], self._cached[2]


def entry_row(
    entry: Entry, status: str, submission: dict[str, Any] | None, hist: list[float] | None
) -> dict[str, Any]:
    scored = status == "scored"
    return {
        "id": submission["id"] if submission else entry.run_id,
        "name": submission["name"] if submission else entry.policy_ref or entry.run_id,
        "handle": submission["handle"] if submission else None,
        "kind": entry.kind or "agent",
        "status": status,
        "day": submission["day"] if submission else None,
        "return_pct": percent(entry.period_return) if scored else None,
        "excess_pct": percent(entry.excess_vs_buy_and_hold) if scored else None,
        "fills": entry.orders_filled if scored else None,
        "history": hist if scored else None,
        "trace_id": entry.trace_id,
    }


def submission_row(submission: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": submission["id"],
        "name": submission["name"],
        "handle": submission["handle"],
        "kind": "agent",
        "status": submission["status"],
        "day": submission["day"],
        "return_pct": None,
        "excess_pct": None,
        "fills": None,
        "history": None,
        "trace_id": None,
    }


def build_board(source: BoardSource, store: Store) -> dict[str, Any]:
    board, histories = source.load()
    by_dir = store.by_run_dir()
    rows = []
    for status, entries in (("scored", board.ranked), ("failed", board.failed)):
        for entry in entries:
            submission = by_dir.get(entry.run_id)
            if submission and submission["hidden"]:
                continue
            rows.append(entry_row(entry, status, submission, histories.get(entry.run_id)))
    rows += [submission_row(s) for s in store.in_flight()]

    # Stable sorts: scored by return (null last), then running, queued (FIFO) and failed.
    rows.sort(key=lambda r: -r["return_pct"] if r["return_pct"] is not None else 0)
    rows.sort(key=lambda r: r["status"] == "scored" and r["return_pct"] is None)
    rows.sort(key=lambda r: STATUS_ORDER[r["status"]])
    scored = 0
    for row in rows:
        if row["status"] == "scored":
            scored += 1
            row["rank"] = scored
        else:
            row["rank"] = None

    window = dict(DEFAULT_WINDOW)
    if board.header is not None:
        window = {
            "start": board.header.start_at.date().isoformat(),
            "end": board.header.end_at.date().isoformat(),
            "starting_cash": float(board.header.starting_cash),
        }
    return {
        "window": {**window, "symbols": SYMBOLS, "days": DAYS},
        "rows": rows,
        "events": store.recent_events(20),
    }
