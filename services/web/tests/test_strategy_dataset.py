"""v10 P1: strategies go to the Logfire dataset "bazaar-strategies", off the request path, with
public fields only."""

import copy
import json
import logging
import sqlite3
import threading

import bazaar_web.dataset
import pytest
from bazaar_web.app import create_app
from bazaar_web.dataset import DATASET, DatasetSync, case_for
from bazaar_web.settings import SECRET_ENV_VARS
from bazaar_web.store import OPERATOR_CONTACT_EMAIL, Store
from fastapi.testclient import TestClient
from logfire.experimental.api_client import DatasetNotFoundError

KEY = "pylf_v1_us_DATASETKEYSENTINEL0123456789"
SECRETS = ("contact-sentinel@example.org", "SentinelCo Industries", "Chief Sentinel Officer")
ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}


class FakeClient:
    """LogfireAPIClient's dataset calls, recorded; add_cases can fail or block."""

    def __init__(self, *, exists=False, fail=None, block=None):
        self.lock = threading.Lock()
        self.calls = []
        self.exists = exists
        self.fail = fail
        self.block = block
        self.blocked = threading.Event()

    def get_dataset(self, id_or_name, *, include_cases):
        self.record("get_dataset", id_or_name, include_cases=include_cases)
        if not self.exists:
            raise DatasetNotFoundError("Not found")

    def create_dataset(self, name, *, description=None):
        self.record("create_dataset", name, description=description)
        self.exists = True

    def add_cases(self, dataset_id_or_name, cases, *, on_conflict):
        self.record("add_cases", dataset_id_or_name, cases=cases, on_conflict=on_conflict)
        if self.block is not None:
            self.blocked.set()
            self.block.wait(10)
        if self.fail is not None:
            raise self.fail

    def record(self, method, *args, **kwargs):
        with self.lock:
            self.calls.append((method, copy.deepcopy(args), copy.deepcopy(kwargs)))

    def pushed(self):
        """Every case sent, in order."""
        with self.lock:
            return [c for m, _, kw in self.calls if m == "add_cases" for c in kw["cases"]]


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(bazaar_web.dataset, "SYNC_S", 0.05)
    monkeypatch.setattr(bazaar_web.dataset, "BACKOFF_MAX_S", 0.4)


@pytest.fixture
def keyed(monkeypatch, quick):
    monkeypatch.setenv("LOGFIRE_API_WRITE_KEY", KEY)


def factory_for(client, keys=None):
    def make(key):
        if keys is not None:
            keys.append(key)
        return client

    return make


def submit(client, name, **contact):
    body = {
        "name": name, "handle": None, "instructions": f"{name}: buy KO on dips, hold MSFT.",
        "email": SECRETS[0], "company": SECRETS[1], "role": SECRETS[2], **contact,
    }  # fmt: skip
    response = client.post("/api/submissions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def synced(path, submission_id):
    with sqlite3.connect(path) as conn:
        (at,) = conn.execute(
            "SELECT dataset_synced_at FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    conn.close()
    return at is not None


def insert(path, rows):
    """Rows straight into the table: (id, name, handle, lane, generation, hidden, email)."""
    Store(path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO submissions (id, name, handle, instructions, ip_hash, status, "
            "created_at, lane, generation, hidden, email) "
            "VALUES (?, ?, ?, ?, 'h', 'scored', '2026-10-12T10:00:00+00:00', ?, ?, ?, ?)",
            [(i, n, h, f"{n}: hold KO all week.", lane, g, hid, e)
             for i, n, h, lane, g, hid, e in rows],
        )  # fmt: skip
    conn.close()


# ---------- the case ----------


def test_a_case_carries_only_the_public_fields(tmp_path):
    store = Store(tmp_path / "web.sqlite3")
    insert(store.path, [("a", "bot-a", "@ann", "human", None, 0, SECRETS[0])])
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE submissions SET company = ?, role = ?", SECRETS[1:])
    conn.close()

    (row,) = store.pending_strategies(10)
    case = case_for(row)

    assert set(row) == {"id", "name", "instructions", "handle", "lane", "generation", "parents",
                        "created_at"}  # fmt: skip
    assert case == {
        "name": "strategy-bot-a",
        "inputs": {
            "name": "bot-a", "instructions": "bot-a: hold KO all week.", "handle": "@ann",
            "lane": "human", "generation": None, "parents": None,
            "submitted_at": "2026-10-12T10:00:00+00:00",
        },
    }  # fmt: skip


def test_scope_is_humans_and_bred_house_strategies(tmp_path, helpers):
    store = Store(tmp_path / "web.sqlite3")
    insert(store.path, [
        ("h1", "attendee-bot", None, "human", None, 0, "a@example.org"),
        ("h2", "house-ko-hold-3", "house", "house", None, 0, None),  # catalog era
        ("h3", "house-xom-ladder-1", "house", "human", None, 0, None),  # before the lane column
        ("h4", "moderated-bot", None, "human", None, 1, "b@example.org"),  # hidden
        ("h5", "smoke-test", None, "human", None, 0, OPERATOR_CONTACT_EMAIL.upper()),
        ("h6", "g0-odd", "house", "house", 0, 0, None),
    ])  # fmt: skip
    batch = [helpers.FakeProposal("g2-bred", "Put $2,000 into KO on day 1 and hold it.",
                                  parents=["attendee-bot"])]  # fmt: skip
    store.add_proposals(batch, generation=2, allowed={"KO"}, parents=["attendee-bot"])
    store.create_house(None)

    rows = store.pending_strategies(100)

    assert [r["name"] for r in rows] == ["attendee-bot", "g2-bred"]
    bred = case_for(rows[1])["inputs"]
    assert (bred["lane"], bred["generation"], bred["parents"]) == ("house", 2, ["attendee-bot"])


# ---------- the loop, on its own ----------


def test_failure_keeps_rows_pending_with_backoff_and_success_clears_it(tmp_path, keyed):
    store = Store(tmp_path / "web.sqlite3")
    insert(store.path, [("a", "bot-a", None, "human", None, 0, None)])
    fake = FakeClient(fail=RuntimeError("logfire said: details"))
    sync = DatasetSync(store, factory_for(fake))

    waits = [sync.round(), sync.round(), sync.round()]
    failing = store.get("a")
    fake.fail = None
    after = sync.round()
    row = store.get("a")

    assert waits == [0.05, 0.1, 0.2]  # doubling from SYNC_S
    assert (failing["dataset_synced_at"], failing["dataset_attempts"]) == (None, 3)
    assert failing["dataset_error_class"] == "RuntimeError"
    assert after == 0.05 and sync.failures == 0
    assert row["dataset_synced_at"] is not None
    assert (row["dataset_attempts"], row["dataset_error_class"]) == (0, None)
    assert sync.backoff() == 0.05  # the backoff starts over
    for _ in range(10):
        sync.backoff()
    assert sync.backoff() == 0.4  # capped


def test_the_dataset_is_created_once_and_cases_upsert_by_name(tmp_path, keyed):
    store = Store(tmp_path / "web.sqlite3")
    insert(store.path, [("a", "bot-a", None, "human", None, 0, None)])
    fake = FakeClient()
    sync = DatasetSync(store, factory_for(fake))

    sync.round()
    with sqlite3.connect(store.path) as conn:  # pending again (a re-push)
        conn.execute("UPDATE submissions SET dataset_synced_at = NULL")
    conn.close()
    sync.round()

    methods = [m for m, _, _ in fake.calls]
    assert methods == ["get_dataset", "create_dataset", "add_cases", "add_cases"]
    assert fake.calls[1][1:] == ((DATASET,), {"description": bazaar_web.dataset.DESCRIPTION})
    adds = [kw for m, _, kw in fake.calls if m == "add_cases"]
    assert [a["on_conflict"] for a in adds] == ["update", "update"]
    assert [c["name"] for c in fake.pushed()] == ["strategy-bot-a", "strategy-bot-a"]


def test_an_existing_dataset_is_not_created_again(tmp_path, keyed):
    store = Store(tmp_path / "web.sqlite3")
    insert(store.path, [("a", "bot-a", None, "human", None, 0, None)])
    fake = FakeClient(exists=True)
    DatasetSync(store, factory_for(fake)).round()
    assert [m for m, _, _ in fake.calls] == ["get_dataset", "add_cases"]


def test_without_a_key_the_factory_is_never_called(tmp_path, quick, caplog):
    store = Store(tmp_path / "web.sqlite3")
    insert(store.path, [("a", "bot-a", None, "human", None, 0, None)])
    keys = []
    sync = DatasetSync(store, factory_for(FakeClient(), keys))
    with caplog.at_level(logging.INFO, logger="bazaar_web.dataset"):
        waits = [sync.round() for _ in range(3)]

    assert keys == [] and waits == [0.05] * 3
    assert store.get("a")["dataset_synced_at"] is None  # waits for a key
    assert [r.getMessage() for r in caplog.records] == ["strategy dataset off"]


# ---------- the loop, in the app ----------


def test_the_backfill_pushes_existing_rows_oldest_first_in_batches(settings, helpers, keyed):
    insert(settings.web_db, [(f"r{i:03d}", f"bot-{i:03d}", None, "human", None, 0, None)
                             for i in range(250)])  # fmt: skip
    fake = FakeClient()
    with TestClient(create_app(settings, helpers.FakeRunner(), dataset_client=factory_for(fake))):
        helpers.wait_for(lambda: len(fake.pushed()) == 250)

    sizes = [len(kw["cases"]) for m, _, kw in fake.calls if m == "add_cases"]
    assert sizes == [100, 100, 50]
    assert [c["name"] for c in fake.pushed()] == [f"strategy-bot-{i:03d}" for i in range(250)]


def test_contact_details_never_leave_and_the_key_never_shows(
    settings, helpers, keyed, caplog, capfire
):
    fake = FakeClient()
    keys = []
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(
            create_app(settings, helpers.FakeRunner(), dataset_client=factory_for(fake, keys))
        ) as client,
    ):
        sid = submit(client, "contact-bot")
        helpers.wait_for(lambda: synced(settings.web_db, sid))
        bodies = [
            client.get(p).text for p in ("/", "/submit", "/api/board", f"/api/submissions/{sid}")
        ]
        bodies.append(client.get("/api/admin/house", headers=ADMIN).text)

    sent = json.dumps(fake.calls, default=str)
    spans = json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)
    assert "strategy-contact-bot" in sent  # it was pushed...
    for secret in SECRETS:  # ...without any contact detail, anywhere
        assert secret not in sent and secret not in caplog.text and secret not in spans
    assert keys == [KEY]  # the key goes to the client only
    assert all(KEY not in body for body in bodies)
    assert KEY not in caplog.text and KEY not in spans
    assert "LOGFIRE_API_WRITE_KEY" in SECRET_ENV_VARS


def test_the_sync_span_carries_counts_only(settings, helpers, keyed, capfire):
    insert(settings.web_db, [("a", "bot-a", None, "human", None, 0, None)])
    fake = FakeClient()
    with TestClient(create_app(settings, helpers.FakeRunner(), dataset_client=factory_for(fake))):
        helpers.wait_for(lambda: synced(settings.web_db, "a"))

    (span,) = [s for s in capfire.exporter.exported_spans_as_dict()
               if s["name"] == "strategy dataset sync"]  # fmt: skip
    attributes = {k: v for k, v in span["attributes"].items() if not k.startswith("logfire.")}
    assert {k: attributes[k] for k in ("pending", "pushed", "failed")} == {
        "pending": 1, "pushed": 1, "failed": 0,
    }  # fmt: skip
    assert "bot-a" not in json.dumps(span, default=str)


def test_a_stuck_push_never_holds_up_submissions(settings, helpers, keyed):
    insert(settings.web_db, [("a", "bot-a", None, "human", None, 0, None)])
    release = threading.Event()
    fake = FakeClient(block=release)
    runner = helpers.FakeRunner()
    try:
        with TestClient(create_app(settings, runner, dataset_client=factory_for(fake))) as client:
            assert fake.blocked.wait(5)  # the sync is stuck inside add_cases
            sid = submit(client, "while-stuck")  # accepted at once
            helpers.wait_for(lambda: client.get(f"/api/submissions/{sid}").json()["status"]
                             == "scored")  # fmt: skip
            assert not release.is_set() and not synced(settings.web_db, sid)
    finally:
        release.set()


def test_without_a_key_submissions_work_and_nothing_is_built(settings, helpers, quick):
    keys = []
    with TestClient(
        create_app(settings, helpers.FakeRunner(), dataset_client=factory_for(FakeClient(), keys))
    ) as client:
        sid = submit(client, "no-key-bot")
        helpers.wait_for(lambda: client.get(f"/api/submissions/{sid}").json()["status"] == "scored")

    assert keys == []
    assert not synced(settings.web_db, sid)


def test_an_old_database_gets_the_dataset_columns(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old', 'old-bot', 'Buy KO.', 'h', 'scored', 't');"
        )
    conn.close()

    store = Store(path)
    Store(path)  # idempotent

    row = store.get("old")
    assert (row["dataset_synced_at"], row["dataset_attempts"], row["dataset_error_class"]) == (
        None, 0, None,
    )  # fmt: skip
    assert [r["name"] for r in store.pending_strategies(10)] == ["old-bot"]  # the backfill
