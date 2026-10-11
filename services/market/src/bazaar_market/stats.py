"""In-process market load figures for GET /control/stats, counted since the process started.

SQLite's busy_timeout waits for a lock silently, so these are waits, not retries. Nothing here
records an id, a token or a symbol.
"""

import threading
from collections import deque
from datetime import UTC, datetime

from fastapi import APIRouter, Depends

SAMPLES = 10_000  # percentiles cover the most recent samples
SLOW_WAIT_MS = 100.0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[round(fraction * (len(ordered) - 1))], 3)


class MarketStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = datetime.now(UTC)
        self._lock_waits: deque[float] = deque(maxlen=SAMPLES)
        self._lock_wait_max = 0.0
        self._slow_waits = 0
        self._locked_errors = 0
        self._order_latencies: deque[float] = deque(maxlen=SAMPLES)
        self._orders = 0

    def lock_wait(self, ms: float) -> None:
        with self._lock:
            self._lock_waits.append(ms)
            self._lock_wait_max = max(self._lock_wait_max, ms)
            self._slow_waits += ms > SLOW_WAIT_MS

    def locked_error(self) -> None:
        with self._lock:
            self._locked_errors += 1

    def order(self, ms: float) -> None:
        with self._lock:
            self._order_latencies.append(ms)
            self._orders += 1

    def snapshot(self) -> dict:
        with self._lock:
            waits, latencies = list(self._lock_waits), list(self._order_latencies)
            return {
                "started_at": self.started_at.isoformat(),
                "write_lock_wait_ms": {
                    "p50": _percentile(waits, 0.5),
                    "p95": _percentile(waits, 0.95),
                    "max": round(self._lock_wait_max, 3) if waits else None,
                },
                "write_lock_waits_over_100ms": self._slow_waits,
                "sqlite_locked_errors": self._locked_errors,
                "order_latency_ms": {
                    "p50": _percentile(latencies, 0.5),
                    "p95": _percentile(latencies, 0.95),
                },
                "orders_total": self._orders,
            }


STATS = MarketStats()


def is_locked(error: Exception) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def build_router(runner_token: str | None) -> APIRouter:
    # Imported here: ledger_api imports db, and db imports this module for STATS.
    from bazaar_market.ledger_api import runner_token_check

    router = APIRouter(dependencies=[Depends(runner_token_check(runner_token))])

    @router.get("/control/stats")
    def stats() -> dict:
        return STATS.snapshot()

    return router
