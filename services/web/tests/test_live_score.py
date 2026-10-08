import sqlite3
from decimal import Decimal

from bazaar_web.app import create_app
from bazaar_web.store import Store
from fastapi.testclient import TestClient

# The submissions table exactly as the first release (W1) created it, before latest_value.
OLD_SCHEMA = """
CREATE TABLE submissions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    handle TEXT,
    instructions TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    day INTEGER,
    error TEXT,
    run_dir TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
    t TEXT NOT NULL,
    text TEXT NOT NULL,
    submission_id TEXT
);
"""


def submit(client, name, ip="10.0.0.1"):
    body = {"name": name, "handle": None, "instructions": "Buy KO on dips, hold MSFT."}
    return client.post("/api/submissions", json=body, headers={"X-Forwarded-For": ip}).json()["id"]


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


def board_row(client, submission_id):
    return {r["id"]: r for r in client.get("/api/board").json()["rows"]}[submission_id]


def test_migration_adds_latest_value_to_an_old_database(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, day, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'running', 3, '2026-10-08T14:00:00+00:00')"
        )
    conn.close()

    Store(path)
    store = Store(path)  # a second start must be a no-op

    columns = [row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(submissions)")]
    assert columns.count("latest_value") == 1
    row = store.get("old1")
    assert (row["name"], row["status"], row["day"], row["latest_value"]) == (
        "old-bot",
        "running",
        3,
        None,
    )
    assert store.set_day("old1", 4, Decimal("10060.00"))
    assert store.get("old1")["latest_value"] == "10060.00"


def test_set_day_keeps_the_last_value_and_only_moves_forward_on_a_new_day(tmp_path):
    store = Store(tmp_path / "web.sqlite3")
    sid = store.create(
        name="bot", handle=None, instructions="x" * 20, ip_hash="h",
        max_queue=30, max_per_day=150, max_per_ip_hour=5,
    )  # fmt: skip
    store.mark_running(sid)

    assert store.set_day(sid, 1, Decimal(10010))
    assert not store.set_day(sid, 1, Decimal(99999))  # same day: unchanged
    assert store.set_day(sid, 2)  # old runner shape: value kept
    assert store.get(sid)["latest_value"] == "10010"
    store.requeue(sid)
    assert store.get(sid)["latest_value"] is None


def test_running_row_shows_a_provisional_return(seeded, helpers):
    runner = helpers.FakeRunner(values={"live-bot": "10060.00"})
    runner.gate.clear()
    with TestClient(create_app(seeded, runner)) as client:
        sid = submit(client, "live-bot")
        helpers.wait_for(lambda: status(client, sid)["day"] == 10)
        live, row = status(client, sid), board_row(client, sid)
        runner.gate.set()

    for payload in (live, row):
        assert payload["status"] == "running"
        assert payload["return_pct"] == 0.6
        assert isinstance(payload["return_pct"], float)
        assert payload["rank"] is None
        assert payload["provisional"] is True
    assert row["excess_pct"] is None


def test_old_runner_shape_still_works_without_a_score(seeded, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    with TestClient(create_app(seeded, runner)) as client:
        sid = submit(client, "old-shape")
        helpers.wait_for(lambda: status(client, sid)["day"] == 10)
        live, row = status(client, sid), board_row(client, sid)
        runner.gate.set()
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")

    assert (live["status"], live["return_pct"], live["provisional"]) == ("running", None, False)
    assert (row["return_pct"], row["provisional"]) == (None, False)


def test_evaluated_return_wins_after_scoring(seeded, helpers):
    runner = helpers.FakeRunner(values={"live-bot": "10060.00"}, returns={"live-bot": "0.0120"})
    with TestClient(create_app(seeded, runner)) as client:
        sid = submit(client, "live-bot")
        done = helpers.wait_for(lambda: (s := status(client, sid))["status"] == "scored" and s)
        row = board_row(client, sid)

    assert (done["return_pct"], done["provisional"], done["rank"]) == (
        1.2,
        False,
        2,
    )  # bh +3.10% is 1st
    assert (row["return_pct"], row["provisional"]) == (1.2, False)


def test_queued_rows_have_no_score_and_running_order_is_fifo(seeded, helpers):
    values = {"first-bot": "9900", "second-bot": "10500", "third-bot": "10100"}
    runner = helpers.FakeRunner(values=values)
    runner.gate.clear()
    with TestClient(create_app(seeded, runner)) as client:
        ids = [submit(client, n, ip=f"10.0.0.{i}") for i, n in enumerate([*values, "fourth-bot"])]
        helpers.wait_for(lambda: all(status(client, i)["day"] == 10 for i in ids[:3]))
        rows = client.get("/api/board").json()["rows"]
        queued = status(client, ids[3])
        runner.gate.set()

    running = [r["name"] for r in rows if r["status"] == "running"]
    assert running == ["first-bot", "second-bot", "third-bot"]  # not re-sorted by live score
    assert [r["return_pct"] for r in rows if r["status"] == "running"] == [-1.0, 5.0, 1.0]
    assert (queued["status"], queued["return_pct"], queued["provisional"]) == (
        "queued",
        None,
        False,
    )
    statuses = [r["status"] for r in rows]
    assert statuses.index("running") > max(i for i, s in enumerate(statuses) if s == "scored")
