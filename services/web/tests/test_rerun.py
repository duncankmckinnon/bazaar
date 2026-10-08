import dataclasses
import json
import sqlite3

import logfire
import pytest
from bazaar_web.app import create_app
from bazaar_web.store import Store
from bazaar_web.worker import expected_run_dir
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}
VALID = {"name": "alice-bot", "handle": "@alice", "instructions": "Buy KO on dips, hold MSFT."}


def submit(client, ip="10.0.0.1", **body):
    return client.post("/api/submissions", json=VALID | body, headers={"X-Forwarded-For": ip})


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


def rerun(client, submission_id, headers=ADMIN):
    return client.post(f"/api/admin/submissions/{submission_id}/rerun", headers=headers)


def board_rows(client):
    return client.get("/api/board").json()["rows"]


def finished(client, helpers, submission_id, state="scored"):
    helpers.wait_for(lambda: status(client, submission_id)["status"] == state)


def test_rerun_of_a_scored_submission_replaces_it_on_the_board(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner(returns={"alice-bot": "0.0120"}))) as c:
        old = submit(c).json()["id"]
        finished(c, helpers, old)
        response = rerun(c, old)
        new = response.json()["id"]
        finished(c, helpers, new)
        rows = board_rows(c)
        old_status = c.get(f"/api/submissions/{old}")

    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    assert isinstance(response.json()["position"], int)
    assert new != old
    store = Store(seeded.web_db)
    old_row, new_row = store.get(old), store.get(new)
    assert (old_row["name"], old_row["hidden"]) == (f"alice-bot~{old[:8]}", 1)
    assert (new_row["name"], new_row["handle"], new_row["instructions"]) == (
        "alice-bot",
        "@alice",
        VALID["instructions"],
    )
    assert (new_row["rerun_of"], new_row["ip_hash"]) == (old, old_row["ip_hash"])
    # The old run is still on disk, complete, but only the rerun is on the board.
    old_dir = seeded.runs_dir / f"sub-{old}"
    assert (old_dir / "record.json").is_file() and (old_dir / "evaluation.json").is_file()
    named = [r for r in rows if r["name"].startswith("alice-bot")]
    assert [(r["id"], r["name"], r["status"]) for r in named] == [(new, "alice-bot", "scored")]
    assert old_status.status_code == 404  # hidden everywhere public


def test_a_failed_submission_can_be_rerun(seeded, helpers):
    runner = helpers.FakeRunner(fail={"alice-bot"})
    with TestClient(create_app(seeded, runner)) as c:
        old = submit(c).json()["id"]
        finished(c, helpers, old, "failed")
        runner.fail = set()
        new = rerun(c, old).json()["id"]
        finished(c, helpers, new)

    assert [call["name"] for call in runner.calls] == ["alice-bot", "alice-bot"]
    assert [call["submission_id"] for call in runner.calls] == [old, new]


@pytest.mark.parametrize("state", ["queued", "running"])
def test_rerun_refuses_a_submission_in_flight(seeded, helpers, state):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    capped = dataclasses.replace(seeded, max_concurrent=1)
    with TestClient(create_app(capped, runner)) as c:
        running = submit(c, name="first-bot").json()["id"]
        helpers.wait_for(lambda: status(c, running)["status"] == "running")
        queued = submit(c, name="second-bot", ip="10.0.0.2").json()["id"]
        target = running if state == "running" else queued
        response = rerun(c, target)
        runner.gate.set()

    assert response.status_code == 409


def test_rerun_admin_guard(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as c:
        old = submit(c).json()["id"]
        finished(c, helpers, old)
        missing = rerun(c, old, headers={})
        wrong = rerun(c, old, headers={"X-Bazaar-Admin-Token": "nope"})
        unknown = rerun(c, "no-such-id")
    with TestClient(
        create_app(dataclasses.replace(settings, admin_token=None), helpers.FakeRunner())
    ) as c:
        disabled = rerun(c, old)

    assert (missing.status_code, wrong.status_code) == (403, 403)
    assert unknown.status_code == 404
    assert disabled.status_code == 404
    assert Store(settings.web_db).get(old)["hidden"] == 0  # refused requests changed nothing


def test_reruns_skip_and_never_consume_the_daily_and_ip_caps(seeded, helpers):
    capped = dataclasses.replace(seeded, max_per_day=2, max_per_ip_hour=1)
    with TestClient(create_app(capped, helpers.FakeRunner())) as c:
        old = submit(c, ip="10.0.0.1").json()["id"]
        finished(c, helpers, old)
        assert submit(c, name="same-ip-bot", ip="10.0.0.1").status_code == 429  # IP cap is full
        first = rerun(c, old)  # same IP, but an admin rerun skips the caps
        finished(c, helpers, first.json()["id"])
        second = rerun(c, first.json()["id"])
        finished(c, helpers, second.json()["id"])
        # Only the original counts toward the day (1 of 2), so another network still gets in.
        later = submit(c, name="later-bot", ip="10.0.0.2")
        finished(c, helpers, later.json()["id"])
        full = submit(c, name="full-bot", ip="10.0.0.3")  # now 2 of 2
        third = rerun(c, second.json()["id"])  # still allowed with the daily cap full

    assert first.status_code == second.status_code == third.status_code == 201
    assert later.status_code == 201
    assert full.status_code == 429
    assert "limit" in full.json()["detail"]
    assert Store(seeded.web_db).get(second.json()["id"])["rerun_of"] == first.json()["id"]


def test_reruns_do_not_count_for_a_later_submission_from_the_same_ip(seeded, helpers):
    capped = dataclasses.replace(seeded, max_per_ip_hour=2)
    with TestClient(create_app(capped, helpers.FakeRunner())) as c:
        old = submit(c, ip="10.0.0.1").json()["id"]
        finished(c, helpers, old)
        new = rerun(c, old).json()["id"]
        finished(c, helpers, new)
        again = rerun(c, new).json()["id"]
        finished(c, helpers, again)
        later = submit(c, name="second-bot", ip="10.0.0.1")

    assert later.status_code == 201  # 1 real + 2 reruns from this IP; only the real one counts


def test_a_rerun_still_counts_toward_the_queue_cap(seeded, helpers):
    runner = helpers.FakeRunner()
    capped = dataclasses.replace(seeded, max_queue=1)
    with TestClient(create_app(capped, runner)) as c:
        old = submit(c).json()["id"]
        finished(c, helpers, old)
        runner.gate.clear()
        rerun(c, old)
        blocked = submit(c, name="other-bot", ip="10.0.0.5")
        runner.gate.set()

    assert blocked.status_code == 429
    assert "queue is full" in blocked.json()["detail"]


def test_the_rerun_gets_new_run_and_experiment_ids(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as c:
        old = submit(c).json()["id"]
        finished(c, helpers, old)
        new = rerun(c, old).json()["id"]

    assert expected_run_dir(seeded.runs_dir, new) != expected_run_dir(seeded.runs_dir, old)


def test_migration_adds_rerun_of_to_an_old_database(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, latest_value TEXT, trace_context TEXT);"
            "CREATE TABLE events (t TEXT NOT NULL, text TEXT NOT NULL, submission_id TEXT);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'scored', '2026-10-08T14:00:00+00:00');"
        )
    conn.close()

    Store(path)
    store = Store(path)

    columns = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(submissions)")]
    assert columns.count("rerun_of") == 1
    assert store.get("old1")["rerun_of"] is None
    new = store.rerun("old1")
    assert (store.get(new)["name"], store.get(new)["rerun_of"]) == ("old-bot", "old1")


def test_the_rerun_is_traced_under_the_admin_request(seeded, helpers, capfire):
    fake = helpers.FakeRunner()

    def runner(**call):
        with logfire.span("runner.run"):
            return fake(**call)

    secret = dataclasses.replace(seeded, admin_token="SENTINEL-ADMIN-123")
    with TestClient(create_app(secret, runner)) as c:
        old = submit(c).json()["id"]
        finished(c, helpers, old)
        capfire.exporter.clear()
        new = rerun(c, old, headers={"X-Bazaar-Admin-Token": "SENTINEL-ADMIN-123"}).json()["id"]
        finished(c, helpers, new)

    spans = capfire.exporter.exported_spans_as_dict()
    [post] = [s for s in spans if s["name"] == "POST /api/admin/submissions/{submission_id}/rerun"]
    [run] = [s for s in spans if s["name"] == "runner.run"]
    [queued] = [s for s in spans if s["name"] == "submission queued"]
    assert (
        run["context"]["trace_id"] == queued["context"]["trace_id"] == post["context"]["trace_id"]
    )
    assert run["parent"]["span_id"] == queued["context"]["span_id"]
    assert queued["attributes"]["submission_id"] == new
    assert "SENTINEL" not in json.dumps(spans, default=str)


def test_a_failed_rerun_leaves_the_old_row_untouched(tmp_path, monkeypatch):
    store = Store(tmp_path / "web.sqlite3")
    common = {"handle": None, "ip_hash": "h", "max_queue": 30, "max_per_day": 150}
    old = store.create(name="old-bot", instructions="x" * 20, max_per_ip_hour=5, **common)
    other = store.create(name="other-bot", instructions="x" * 20, max_per_ip_hour=5, **common)
    store.finish(old, run_dir="d", error=None)
    # The new row's id collides with an existing one, so the INSERT fails after the rename.
    monkeypatch.setattr("bazaar_web.store.uuid4", lambda: type("U", (), {"hex": other})())

    with pytest.raises(sqlite3.IntegrityError):
        store.rerun(old)

    row = store.get(old)
    assert (row["name"], row["hidden"], row["status"]) == ("old-bot", 0, "scored")
