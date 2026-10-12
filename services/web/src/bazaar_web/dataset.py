"""Every in-scope strategy as a case in the Logfire managed dataset "bazaar-strategies" (v10 P1).

Logfire has no public API to create prompts, so each strategy is a dataset case (Anthony's P1
decision). A background loop on its own thread pushes pending rows in batches. It is off the
request path: POST /api/submissions only inserts, and a Logfire outage only delays the loop.
The first rounds after a deploy are also the one-time backfill, since existing rows start
pending.

In scope: human-lane submissions and the strategy factory's house rows (generation > 0). Not in
scope: catalog-era house rows (duplicates), hidden rows and operator rows. A case carries only
public fields, read from an explicit column list; contact details are never selected.

Hiding is moderation of untrusted text, so a hidden row's case is deleted: each round first
deletes the cases of rows hidden since their push (by the case id stored at push time), then
pushes. A row hidden before its push is never sent.

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
    ) -> list[Any]: ...

    def delete_case(self, dataset_id_or_name: str, case_id: str) -> None: ...


ClientFactory = Callable[[str], DatasetClient]


def logfire_client(key: str) -> DatasetClient:
    from logfire.experimental.api_client import LogfireAPIClient

    return LogfireAPIClient(api_key=key)


def case_name(name: str, submission_id: str) -> str:
    """Stable per row, so a re-push upserts, and never shared between rows: an admin rerun gives
    the new row the old row's name (the old one is renamed and hidden), so the id goes in too."""
    return f"strategy-{name}-{submission_id[:8]}"


def case_for(row: dict[str, Any]) -> dict[str, Any]:
    """A dataset case from the public columns Store.pending_strategies selects."""
    parents = json.loads(row["parents"]) if row["parents"] else None
    return {
        "name": case_name(row["name"], row["id"]),
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


def is_gone(exc: Exception) -> bool:
    from logfire.experimental.api_client import CaseNotFoundError

    return isinstance(exc, CaseNotFoundError)


class MissingCaseId(Exception):
    """add_cases returned no case for a row we sent: the row stays unsynced."""


class Failed(Exception):
    """A pass failed for these rows, with this exception class."""

    def __init__(self, ids: list[str], error: str) -> None:
        super().__init__(error)
        self.ids, self.error = ids, error


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
        """Delete hidden rows' cases, then push one batch; the seconds to wait before the next
        round."""
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            if not self.said_off:
                log.info("strategy dataset off")
                self.said_off = True
            return SYNC_S
        with logfire.suppress_instrumentation():
            deletes = self.store.pending_deletes(BATCH)
            rows = self.store.pending_strategies(BATCH)
        if not deletes and not rows:
            return SYNC_S
        counts = {"pending": len(rows), "pushed": 0, "deleted": 0, "failed": 0}
        with logfire.span("strategy dataset sync", **counts) as span:
            try:
                counts["deleted"] = self.delete(deletes)
                counts["pushed"] = self.push(rows)
            except Failed as failure:
                with logfire.suppress_instrumentation():
                    self.store.dataset_failed(failure.ids, failure.error)
                counts["failed"] = len(failure.ids)
                span.set_attributes({**counts, "error_class": failure.error})
                # The class only: a client error's text could quote a response.
                log.warning(
                    "strategy dataset sync failed (%s); %d rows wait",
                    failure.error,
                    len(failure.ids),
                )
                return self.backoff()
            span.set_attributes(counts)
        self.failures = 0
        full = len(rows) == BATCH or len(deletes) == BATCH
        return 0.0 if full else SYNC_S  # a full batch: more are likely waiting

    def connect(self) -> DatasetClient:
        if self.client is None:
            self.client = self.client_factory(os.environ[KEY_ENV].strip())
        return self.client

    def delete(self, rows: list[dict[str, Any]]) -> int:
        """Delete each hidden row's case; a case already gone counts as deleted."""
        done = 0
        for row in rows:
            try:
                self.connect().delete_case(DATASET, row["dataset_case_id"])
            except Exception as exc:  # noqa: BLE001 - classified below
                if not is_gone(exc):
                    raise Failed([row["id"]], type(exc).__name__) from None
            with logfire.suppress_instrumentation():
                self.store.dataset_deleted([row["id"]])
            done += 1
        return done

    def push(self, rows: list[dict[str, Any]]) -> int:
        """Upsert the rows' cases and store each returned case id; a row the reply leaves out
        stays unsynced, so no row counts as synced without an id."""
        if not rows:
            return 0
        ids = [row["id"] for row in rows]
        try:
            client = self.connect()
            if not self.ensured:
                try:
                    client.get_dataset(DATASET, include_cases=False)
                except Exception as exc:
                    if not is_missing(exc):
                        raise
                    client.create_dataset(DATASET, description=DESCRIPTION)
                self.ensured = True
            cases = [case_for(row) for row in rows]
            returned = client.add_cases(DATASET, cases=cases, on_conflict="update")
        except Exception as exc:  # noqa: BLE001 - Logfire down, bad key, anything
            raise Failed(ids, type(exc).__name__) from None
        by_name = {c.get("name"): str(c["id"]) for c in returned or () if c.get("id")}
        found = {row["id"]: by_name[case["name"]] for row, case in zip(rows, cases, strict=True)
                 if case["name"] in by_name}  # fmt: skip
        with logfire.suppress_instrumentation():
            self.store.dataset_synced(found)
        missing = [i for i in ids if i not in found]
        if missing:
            raise Failed(missing, MissingCaseId.__name__)
        return len(found)
