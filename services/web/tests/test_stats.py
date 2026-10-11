import dataclasses
import json
import logging
import sqlite3
from datetime import UTC, datetime

import pytest
from bazaar_web import store as store_module
from bazaar_web.app import create_app
from bazaar_web.settings import Settings
from bazaar_web.stats import nearest_rank, parse_time, summarize
from bazaar_web.store import LOCK_ERRORS, Store
from bazaar_web.worker import FAILED
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}


def body(name):
    return {"name": name, "handle": None, "instructions": "Buy KO on dips, hold MSFT."}


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


# ---------- BAZAAR_MAX_CONCURRENT ----------


def test_max_concurrent_defaults_to_3_and_reads_the_env(monkeypatch):
    monkeypatch.delenv("BAZAAR_MAX_CONCURRENT", raising=False)
    assert Settings.from_env().max_concurrent == 3
    monkeypatch.setenv("BAZAAR_MAX_CONCURRENT", "6")
    assert Settings.from_env().max_concurrent == 6
    monkeypatch.setenv("BAZAAR_MAX_CONCURRENT", " 32 ")
    assert Settings.from_env().max_concurrent == 32


@pytest.mark.parametrize("raw", ["0", "33", "-1", "", "six", "2.5", "1e1"])
def test_max_concurrent_outside_1_to_32_is_refused_at_startup(monkeypatch, raw):
    monkeypatch.setenv("BAZAAR_MAX_CONCURRENT", raw)
    with pytest.raises(ValueError, match="BAZAAR_MAX_CONCURRENT must be an integer from 1 to 32"):
        Settings.from_env()


def test_six_runs_at_once_and_the_seventh_waits(settings, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    six = dataclasses.replace(settings, max_concurrent=6, max_per_ip_hour=10)
    with TestClient(create_app(six, runner)) as client:
        ids = [
            client.post("/api/submissions", json=body(f"bot-{i}")).json()["id"] for i in range(7)
        ]
        helpers.wait_for(lambda: len(runner.calls) == 6)
        statuses = [status(client, i)["status"] for i in ids]
        seventh = status(client, ids[6])
        stats = client.get("/api/admin/stats", headers=ADMIN).json()["aggregates"]
        runner.gate.set()
        helpers.wait_for(lambda: status(client, ids[6])["status"] == "scored")

    assert statuses == ["running"] * 6 + ["queued"]
    assert seventh["position"] == 1
    assert (stats["in_flight"], stats["max_concurrent"]) == (6, 6)
    assert len(runner.calls) == 7


def test_runs_beyond_the_default_executor_size_all_execute(settings, helpers, monkeypatch):
    # With one CPU the loop's default executor has 5 threads; 8 runs must still all start.
    monkeypatch.setattr("os.process_cpu_count", lambda: 1)
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    runner = helpers.FakeRunner()
    runner.gate.clear()
    eight = dataclasses.replace(settings, max_concurrent=8, max_per_ip_hour=10)
    with TestClient(create_app(eight, runner)) as client:
        ids = [
            client.post("/api/submissions", json=body(f"bot-{i}")).json()["id"] for i in range(8)
        ]
        helpers.wait_for(lambda: len(runner.calls) == 8)
        runner.gate.set()
        helpers.wait_for(lambda: all(status(client, i)["status"] == "scored" for i in ids))


# ---------- /api/admin/stats ----------


def test_stats_needs_the_admin_token(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert client.get("/api/admin/stats").status_code == 403
        wrong = client.get("/api/admin/stats", headers={"X-Bazaar-Admin-Token": "nope"})
        assert wrong.status_code == 403
        assert client.get("/api/admin/stats", headers=ADMIN).status_code == 200
        bad_since = client.get("/api/admin/stats?since=yesterday", headers=ADMIN)
        assert bad_since.status_code == 422
    unset = dataclasses.replace(settings, admin_token=None)
    with TestClient(create_app(unset, helpers.FakeRunner())) as client:
        assert client.get("/api/admin/stats", headers=ADMIN).status_code == 404


def t(minute: int, second: int = 0) -> str:
    return datetime(2026, 10, 10, 14, minute, second, tzinfo=UTC).isoformat()


def stats_db(path):
    """Four scored rows with known waits and run times, one failure, one queued, one old row."""
    store = Store(path)
    rows = [
        # name, status, created, started, finished, error_class
        ("a", "scored", t(0), t(0, 10), t(5, 10), None),  # wait 10, run 300
        ("b", "scored", t(0), t(0, 20), t(10, 20), None),  # wait 20, run 600
        ("c", "scored", t(0), t(1), t(16), None),  # wait 60, run 900
        ("d", "scored", t(10), t(12), t(30), None),  # wait 120, run 1080
        ("e", "failed", t(20), t(20, 30), t(21), "TimeoutError"),
        ("f", "queued", t(40), None, None, None),
        ("g", "running", t(41), t(41, 5), None, None),
        (
            "old",
            "scored",
            "2026-10-10T01:00:00+00:00",
            "2026-10-10T01:00:05+00:00",
            "2026-10-10T01:05:00+00:00",
            None,
        ),  # entirely before since
    ]
    with sqlite3.connect(path) as conn:
        for name, st, created, started, finished, error_class in rows:
            conn.execute(
                "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at, "
                "started_at, finished_at, error, error_class) "
                "VALUES (?, ?, 'secret instructions', 'secret-hash', ?, ?, ?, ?, ?, ?)",
                (f"id-{name}", name, st, created, started, finished,
                 FAILED if error_class else None, error_class),
            )  # fmt: skip
    conn.close()
    return store


def test_stats_math_on_known_timestamps(tmp_path):
    store = stats_db(tmp_path / "web.sqlite3")

    # Through summarize: starting the app would recover "g" and run "f".
    payload = summarize(store.timings(), since=parse_time(t(0)), max_concurrent=3, lock_errors=4)
    agg = payload["aggregates"]
    assert payload["window"] == {"since": t(0)}
    by_name = {s["name"]: s for s in payload["submissions"]}
    assert "old" not in by_name
    assert set(by_name["a"]) == {
        "id", "name", "status", "created_at", "started_at", "finished_at",
        "queue_wait_s", "run_s", "error_class", "rerun_of", "lane",
    }  # fmt: skip
    assert "secret" not in json.dumps(payload)
    assert (by_name["d"]["queue_wait_s"], by_name["d"]["run_s"]) == (120.0, 1080.0)
    assert by_name["e"]["error_class"] == "TimeoutError"
    # Scored run times 300, 600, 900, 1080: nearest rank p50 = 2nd, p95 = 4th.
    assert agg["run_s"] == {"p50": 600.0, "p95": 1080.0}
    assert agg["queue_wait_s"] == {"p50": 20.0, "p95": 120.0}
    # 4 scored between the first start (14:00:10) and the last finish (14:30:00).
    assert agg["runs_per_hour"] == pytest.approx(4 / ((30 * 60 - 10) / 3600))
    assert agg["failures_by_class"] == {"TimeoutError": 1}
    assert agg["count_by_status"] == {"scored": 4, "failed": 1, "queued": 1, "running": 1}
    assert agg["in_flight"] == 1
    assert agg["sqlite_locked_errors"] == 4
    assert agg["max_concurrent"] == 3


def test_the_default_window_is_the_last_six_hours(settings, helpers):
    stats_db(settings.web_db)
    with sqlite3.connect(settings.web_db) as conn:
        conn.execute("DELETE FROM submissions WHERE status IN ('queued', 'running')")
    conn.close()
    clock = helpers.Clock()
    clock.at = datetime(2026, 10, 10, 20, 0, 30, tzinfo=UTC)  # 6h after 14:00:30
    with TestClient(create_app(settings, helpers.FakeRunner(), now=clock)) as client:
        payload = client.get("/api/admin/stats", headers=ADMIN).json()

    assert payload["window"]["since"] == t(0, 30)
    # a, b and c were created before since but finished after it, so they count.
    assert {s["name"] for s in payload["submissions"]} == {"a", "b", "c", "d", "e"}
    assert payload["aggregates"]["run_s"] == {"p50": 600.0, "p95": 1080.0}
    with TestClient(create_app(settings, helpers.FakeRunner(), now=clock)) as client:
        for since in (t(0), t(0).replace("+00:00", "Z"), "2026-10-10T14:00:00"):
            plain = client.get(f"/api/admin/stats?since={since}", headers=ADMIN)  # "+" unencoded
            assert plain.json()["window"]["since"] == t(0), since


def test_percentiles_need_two_values():
    assert nearest_rank([], 50) is None
    assert nearest_rank([7.0], 95) is None
    assert nearest_rank([3.0, 1.0], 50) == 1.0
    assert nearest_rank([3.0, 1.0], 95) == 3.0
    assert nearest_rank([float(v) for v in range(1, 21)], 95) == 19.0


def test_stats_are_not_traced(settings, helpers, capfire):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        capfire.exporter.clear()
        client.get("/api/admin/stats", headers=ADMIN)
        client.get(f"/api/admin/stats?since={t(0)}", headers=ADMIN)

    assert capfire.exporter.exported_spans_as_dict() == []


# ---------- error_class ----------


def test_a_failed_run_records_its_class_and_keeps_the_public_error(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner(fail={"bad-bot"}))) as client:
        sid = client.post("/api/submissions", json=body("bad-bot")).json()["id"]
        failed = helpers.wait_for(lambda: (s := status(client, sid))["status"] == "failed" and s)
        stats = client.get("/api/admin/stats", headers=ADMIN).json()

    assert failed["error"] == FAILED
    row = Store(settings.web_db).get(sid)
    assert (row["error"], row["error_class"]) == (FAILED, "RuntimeError")
    [entry] = stats["submissions"]
    assert entry["error_class"] == "RuntimeError"
    assert stats["aggregates"]["failures_by_class"] == {"RuntimeError": 1}
    assert "super-secret-token" not in json.dumps(stats)


def test_migration_adds_error_class_to_an_old_database(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, latest_value TEXT, trace_context TEXT, "
            "rerun_of TEXT);"
            "CREATE TABLE events (t TEXT NOT NULL, text TEXT NOT NULL, submission_id TEXT);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'running', '2026-10-08T14:00:00+00:00');"
        )
    conn.close()

    store = Store(path)
    Store(path)  # idempotent

    assert store.get("old1")["error_class"] is None
    store.finish("old1", run_dir=None, error=FAILED, error_class="SubmissionFailed")
    assert store.get("old1")["error_class"] == "SubmissionFailed"
    assert store.get("old1")["name"] == "old-bot"


# ---------- sqlite lock counter ----------


class LockedCursor:
    def execute(self, sql, params):
        raise sqlite3.OperationalError("database is locked")


class LockedConnection:
    def cursor(self):
        return LockedCursor()


def test_a_locked_database_is_counted_logged_and_re_raised(settings, monkeypatch, caplog):
    store = Store(settings.web_db)
    before = LOCK_ERRORS.count
    monkeypatch.setattr(store_module.logfire, "instrument_sqlite3", lambda conn: LockedConnection())

    with caplog.at_level(logging.WARNING, logger="bazaar_web.store"):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.get("anything")
        monkeypatch.undo()
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            store._connect().execute("SELECT * FROM nope")  # other errors are not counted

    assert LOCK_ERRORS.count == before + 1
    assert [r.getMessage() for r in caplog.records] == ["sqlite lock error: database is locked"]


def test_a_busy_begin_is_counted_too(settings):
    store = Store(settings.web_db)
    before = LOCK_ERRORS.count
    holder = sqlite3.connect(settings.web_db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    store_connect = store._connect

    def quick(*_):
        conn = store_connect()
        conn.raw.execute("PRAGMA busy_timeout = 0")
        return conn

    store._connect = quick
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.add_event("blocked")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert LOCK_ERRORS.count == before + 1
