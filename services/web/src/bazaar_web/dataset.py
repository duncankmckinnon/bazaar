"""Every in-scope strategy as a case in the Logfire managed dataset "bazaar-strategies" (v10 P1).

Logfire has no public API to create prompts, so each strategy is a dataset case (Anthony's P1
decision). A background loop on its own thread pushes pending rows in batches. It is off the
request path: POST /api/submissions only inserts, and a Logfire outage only delays the loop.
The first rounds after a deploy are also the one-time backfill, since existing rows start
pending.

In scope: human-lane submissions and the strategy factory's house rows (generation > 0). Not in
scope: catalog-era house rows (duplicates), hidden rows and operator rows. A case carries only
public fields, read from an explicit column list; contact details are never selected.

The key (LOGFIRE_API_WRITE_KEY, scopes project:read_datasets and project:write_datasets) is read
from the environment when the loop pushes. Without it nothing is attempted, and rows wait.
"""

import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any, Protocol

import logfire

from bazaar_web.store import Store

log = logging.getLogger("bazaar_web.dataset")

DATASET = "bazaar-strategies"
DESCRIPTION = "Every Bazaar strategy: attendee submissions and the house factory's bred ones."
KEY_ENV = "LOGFIRE_API_WRITE_KEY"
BATCH = 100
SYNC_S = 15.0  # between rounds, while there is nothing left to push
BACKOFF_MAX_S = 300.0


class DatasetClient(Protocol):
    """The part of logfire.experimental.api_client.LogfireAPIClient the loop uses."""

    def get_dataset(self, id_or_name: str, *, include_cases: bool) -> Any: ...

    def create_dataset(self, name: str, *, description: str | None = None) -> Any: ...

    def add_cases(
        self, dataset_id_or_name: str, cases: list[dict[str, Any]], *, on_conflict: str
    ) -> Any: ...


ClientFactory = Callable[[str], DatasetClient]


def logfire_client(key: str) -> DatasetClient:
    from logfire.experimental.api_client import LogfireAPIClient

    return LogfireAPIClient(api_key=key)


def case_name(name: str) -> str:
    """Stable per strategy (submission names are unique), so a re-push upserts."""
    return f"strategy-{name}"


def case_for(row: dict[str, Any]) -> dict[str, Any]:
    """A dataset case from the public columns Store.pending_strategies selects."""
    parents = json.loads(row["parents"]) if row["parents"] else None
    return {
        "name": case_name(row["name"]),
        "inputs": {
            "name": row["name"],
            "instructions": row["instructions"],
            "handle": row["handle"],
            "lane": row["lane"] or "human",
            "generation": row["generation"],
            "parents": parents,
            "submitted_at": row["created_at"],
        },
    }


def is_missing(exc: Exception) -> bool:
    from logfire.experimental.api_client import DatasetNotFoundError

    return isinstance(exc, DatasetNotFoundError)


class DatasetSync:
    """The push loop. run() blocks until stop(), so it runs on a thread of its own."""

    def __init__(self, store: Store, client_factory: ClientFactory | None = None) -> None:
        self.store = store
        self.client_factory = client_factory or logfire_client
        self.stopping = threading.Event()
        self.failures = 0  # consecutive failed pushes
        self.client: DatasetClient | None = None
        self.ensured = False  # the dataset exists (checked once per process)
        self.said_off = False

    def stop(self) -> None:
        self.stopping.set()

    def run(self) -> None:
        wait = 0.0  # the first round (the backfill) starts at once
        while not self.stopping.wait(wait):
            try:
                wait = self.round()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any surprise
                log.warning("strategy dataset round failed (%s)", type(exc).__name__)
                wait = self.backoff()

    def backoff(self) -> float:
        self.failures += 1
        return min(SYNC_S * 2 ** (self.failures - 1), BACKOFF_MAX_S)

    def round(self) -> float:
        """Push one batch; the seconds to wait before the next round."""
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            if not self.said_off:
                log.info("strategy dataset off")
                self.said_off = True
            return SYNC_S
        with logfire.suppress_instrumentation():
            rows = self.store.pending_strategies(BATCH)
        if not rows:
            return SYNC_S
        with logfire.span("strategy dataset sync", pending=len(rows)) as span:
            try:
                self.push([case_for(row) for row in rows])
            except Exception as exc:  # noqa: BLE001 - Logfire down, bad key, anything
                error = type(exc).__name__
                with logfire.suppress_instrumentation():
                    self.store.dataset_failed([row["id"] for row in rows], error)
                span.set_attributes({"pushed": 0, "failed": len(rows), "error_class": error})
                # The class only: a client error's text could quote a response.
                log.warning("strategy dataset push failed (%s); %d rows wait", error, len(rows))
                return self.backoff()
            with logfire.suppress_instrumentation():
                self.store.dataset_synced([row["id"] for row in rows])
            span.set_attributes({"pushed": len(rows), "failed": 0})
        self.failures = 0
        return 0.0 if len(rows) == BATCH else SYNC_S  # a full batch: more are likely waiting

    def push(self, cases: list[dict[str, Any]]) -> None:
        if self.client is None:
            self.client = self.client_factory(os.environ[KEY_ENV].strip())
        if not self.ensured:
            try:
                self.client.get_dataset(DATASET, include_cases=False)
            except Exception as exc:
                if not is_missing(exc):
                    raise
                self.client.create_dataset(DATASET, description=DESCRIPTION)
            self.ensured = True
        self.client.add_cases(DATASET, cases=cases, on_conflict="update")
