"""Throughput stats for the admin: queue waits, run times and failures over a time window."""

import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any


def parse_time(value: str) -> datetime:
    """An ISO timestamp; one without an offset is read as UTC."""
    at = datetime.fromisoformat(value)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def seconds(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    return (parse_time(end) - parse_time(start)).total_seconds()


def nearest_rank(values: list[float], pct: int) -> float | None:
    """The nearest-rank percentile, or None with fewer than 2 values."""
    if len(values) < 2:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def in_window(row: dict[str, Any], since: datetime) -> bool:
    """Created or finished at or after since."""
    return any(
        row[key] is not None and parse_time(row[key]) >= since
        for key in ("created_at", "finished_at")
    )


def summarize(
    rows: list[dict[str, Any]], *, since: datetime, max_concurrent: int, lock_errors: int
) -> dict[str, Any]:
    """The /api/admin/stats payload. rows come from Store.timings (no instructions or ip hash).

    in_flight counts every running submission, inside the window or not; the rest is windowed.
    sqlite_locked_errors counts raised lock errors (the market's /control/stats name); no retries.
    Percentiles and runs per hour use the window's scored rows only.
    """
    window = [row for row in rows if in_window(row, since)]
    submissions = [
        {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "queue_wait_s": seconds(row["created_at"], row["started_at"]),
            "run_s": seconds(row["started_at"], row["finished_at"]),
            "error_class": row["error_class"],
            "rerun_of": row["rerun_of"],
        }
        for row in window
    ]
    scored = [s for s in submissions if s["status"] == "scored"]
    run_s = [s["run_s"] for s in scored if s["run_s"] is not None]
    waits = [s["queue_wait_s"] for s in scored if s["queue_wait_s"] is not None]
    return {
        "window": {"since": since.isoformat()},
        "submissions": submissions,
        "aggregates": {
            "count_by_status": dict(Counter(s["status"] for s in submissions)),
            "runs_per_hour": runs_per_hour(scored),
            "run_s": {"p50": nearest_rank(run_s, 50), "p95": nearest_rank(run_s, 95)},
            "queue_wait_s": {"p50": nearest_rank(waits, 50), "p95": nearest_rank(waits, 95)},
            "failures_by_class": dict(
                Counter(
                    s["error_class"] or "unknown" for s in submissions if s["status"] == "failed"
                )
            ),
            "in_flight": sum(row["status"] == "running" for row in rows),
            "max_concurrent": max_concurrent,
            "sqlite_locked_errors": lock_errors,
        },
    }


def runs_per_hour(scored: list[dict[str, Any]]) -> float | None:
    """Scored runs over the hours from the first start to the last finish; None if no span."""
    starts = [parse_time(s["started_at"]) for s in scored if s["started_at"]]
    ends = [parse_time(s["finished_at"]) for s in scored if s["finished_at"]]
    if not starts or not ends:
        return None
    hours = (max(ends) - min(starts)).total_seconds() / 3600
    return len(scored) / hours if hours > 0 else None
