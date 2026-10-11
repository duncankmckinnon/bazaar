import dataclasses
import sqlite3
import threading
import time
from datetime import timedelta

import bazaar_web.worker
import pytest
from bazaar_web.app import create_app
from bazaar_web.house import catalog, house_name
from bazaar_web.settings import Settings
from bazaar_web.store import CapReached, Store
from bazaar_web.worker import expected_run_dir
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}


def body(name):
    return {"name": name, "handle": None, "instructions": "Buy KO on dips, hold MSFT."}


class LaneRunner:
    """A fake run_submission whose runs block until released (by name), or finish at once."""

    def __init__(self, helpers, *, auto=False):
        self.helpers = helpers
        self.auto = auto
        self.lock = threading.Lock()
        self.gates: dict[str, threading.Event] = {}
        self.active: set[str] = set()
        self.started: list[str] = []

    def gate(self, name):
        with self.lock:
            return self.gates.setdefault(name, threading.Event())

    def release(self, name):
        self.gate(name).set()

    def running(self, prefix=""):
        with self.lock:
            return sorted(n for n in self.active if n.startswith(prefix))

    def __call__(self, *, submission_id, name, runs_dir, on_progress=None, **_):
        with self.lock:
            self.active.add(name)
            self.started.append(name)
        try:
            if not self.auto and not self.gate(name).wait(timeout=10):
                raise TimeoutError("test gate never opened")
            return self.helpers.write_run(
                runs_dir,
                expected_run_dir(runs_dir, submission_id).name,
                policy_ref=f"submission-{name}",
                period_return="0.0100",
            )
        finally:
            with self.lock:
                self.active.discard(name)


@pytest.fixture(autouse=True)
def quick_idle(monkeypatch):
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_IDLE_S", 0.02)


def lanes(settings, **changes):
    defaults = {"house_enabled": True, "house_concurrent": 2, "max_per_ip_hour": 20}
    return dataclasses.replace(settings, **(defaults | changes))


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


# ---------- settings ----------


def test_house_settings_from_env(monkeypatch):
    for name in ("BAZAAR_HOUSE_ENABLED", "BAZAAR_HOUSE_CONCURRENT", "BAZAAR_HOUSE_MAX_PER_HOUR"):
        monkeypatch.delenv(name, raising=False)
    default = Settings.from_env()
    assert (default.house_enabled, default.house_concurrent, default.house_max_per_hour) == (
        False,
        3,
        None,
    )
    monkeypatch.setenv("BAZAAR_HOUSE_ENABLED", "true")
    monkeypatch.setenv("BAZAAR_HOUSE_CONCURRENT", "0")
    monkeypatch.setenv("BAZAAR_HOUSE_MAX_PER_HOUR", "40")
    on = Settings.from_env()
    assert (on.house_enabled, on.house_concurrent, on.house_max_per_hour) == (True, 0, 40)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("BAZAAR_HOUSE_ENABLED", "maybe"),
        ("BAZAAR_HOUSE_CONCURRENT", "17"),
        ("BAZAAR_HOUSE_CONCURRENT", "-1"),
        ("BAZAAR_HOUSE_CONCURRENT", "three"),
        ("BAZAAR_HOUSE_MAX_PER_HOUR", "0"),
    ],
)
def test_bad_house_settings_are_refused_at_startup(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


# ---------- the lanes ----------


def test_humans_never_wait_behind_house_runs(settings, helpers):
    runner = LaneRunner(helpers)
    with TestClient(create_app(lanes(settings, max_concurrent=1), runner)) as client:
        helpers.wait_for(lambda: len(runner.running("house-")) == 2)
        first = client.post("/api/submissions", json=body("human-one")).json()["id"]
        helpers.wait_for(lambda: runner.running("human-") == ["human-one"])
        second = client.post("/api/submissions", json=body("human-two")).json()
        assert second["position"] == 1  # queued behind the human, not the house runs
        runner.release("human-one")
        helpers.wait_for(lambda: runner.running("human-") == ["human-two"])
        assert len(runner.running("house-")) == 2  # both lanes full; the house kept its slots
        assert status(client, first)["status"] == "scored"
        for name in runner.running():
            runner.release(name)

    houses = [n for n in runner.started if n.startswith("house-")]
    assert len(houses) >= 2  # the house never took the freed human slot


def test_the_house_lane_stays_full_and_refills(settings, helpers):
    runner = LaneRunner(helpers)
    with TestClient(create_app(lanes(settings, house_concurrent=3), runner)) as client:
        helpers.wait_for(lambda: len(runner.running("house-")) == 3)
        first = runner.running("house-")[0]
        runner.release(first)
        helpers.wait_for(lambda: len(runner.started) == 4 and len(runner.running()) == 3)
        state = client.get("/api/admin/house", headers=ADMIN).json()
        board = client.get("/api/board").json()["rows"]
        for name in runner.running():
            runner.release(name)

    assert (state["running"], state["total_started"], state["slots"]) == (3, 4, 3)
    house_rows = [r for r in board if r["lane"] == "house"]
    assert {r["status"] for r in house_rows} == {"running", "scored"}  # never sat queued
    assert all(r["handle"] == "house" for r in house_rows)
    assert [n for n in runner.started] == [house_name(s, 1) for s, _ in catalog()[:4]]


@pytest.mark.parametrize("change", [{"house_enabled": False}, {"house_concurrent": 0}])
def test_the_kill_switch_means_no_house_runs(settings, helpers, change):
    runner = LaneRunner(helpers, auto=True)
    with TestClient(create_app(lanes(settings, **change), runner)) as client:
        sid = client.post("/api/submissions", json=body("human-only")).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        state = client.get("/api/admin/house", headers=ADMIN).json()

    assert runner.started == ["human-only"]
    assert (state["enabled"], state["total_started"], state["slots"]) == (False, 0, 0)


def test_pause_and_resume_persist_across_restarts(settings, helpers):
    config = lanes(settings, house_concurrent=1)
    runner = LaneRunner(helpers)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.running("house-")) == 1)
        paused = client.post("/api/admin/house/pause", headers=ADMIN).json()
        runner.release(runner.running()[0])  # the running one finishes; no new one starts
        helpers.wait_for(lambda: runner.running() == [])
        assert paused["paused"] is True

    runner = LaneRunner(helpers)
    with TestClient(create_app(config, runner)) as client:
        assert client.get("/api/admin/house", headers=ADMIN).json()["paused"] is True
        assert runner.started == []  # still paused after the restart
        resumed = client.post("/api/admin/house/resume", headers=ADMIN).json()
        helpers.wait_for(lambda: len(runner.running("house-")) == 1)
        runner.release(runner.running()[0])

    assert resumed["paused"] is False
    assert runner.started == [house_name(catalog()[1][0], 1)]  # the cursor moved on


def test_the_cursor_survives_a_restart_and_names_never_repeat(settings, helpers):
    config = lanes(settings, house_concurrent=2)
    names = []
    for _ in range(2):
        runner = LaneRunner(helpers, auto=True)
        with TestClient(create_app(config, runner)) as client:
            helpers.wait_for(lambda r=runner: len(r.started) >= 5)
            client.post("/api/admin/house/pause", headers=ADMIN)
            helpers.wait_for(lambda r=runner: r.running() == [])
            client.post("/api/admin/house/resume", headers=ADMIN)
            client.post("/api/admin/house/pause", headers=ADMIN)
            helpers.wait_for(lambda r=runner: r.running() == [])
        names += runner.started
        Store(config.web_db).set_house_paused(False)

    assert len(names) == len(set(names))
    assert names == [house_name(s, 1) for s, _ in catalog()[: len(names)]]


def test_the_cycle_goes_up_after_the_catalog_and_taken_names_are_skipped(tmp_path):
    store = Store(tmp_path / "web.sqlite3")
    tiny = [("a", "Buy KO on day 1 and hold it."), ("b", "Buy XOM on day 1 and hold it.")]
    store.create(
        name="house-b-1", handle=None, instructions="x" * 20, ip_hash="h",
        max_queue=30, max_per_day=150, max_per_ip_hour=5,
    )  # fmt: skip

    made = [store.create_house(tiny, None)[1] for _ in range(4)]

    assert made == ["house-a-1", "house-a-2", "house-b-2", "house-a-3"]
    state = store.house_state()
    assert (state["cursor"], state["cycle"], state["total_started"]) == (1, 3, 4)


def test_house_rows_count_toward_no_cap(settings, helpers):
    config = lanes(settings, house_concurrent=3, max_queue=1, max_per_ip_hour=1, max_per_day=1)
    runner = LaneRunner(helpers)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.running("house-")) == 3)
        human = client.post("/api/submissions", json=body("human-one"))
        second = client.post("/api/submissions", json=body("human-two"))
        for name in ["human-one", *runner.running("house-")]:
            runner.release(name)

    assert human.status_code == 201  # three house runs in flight did not fill a cap of 1
    assert second.status_code == 429  # the human's own run did


def test_the_hourly_cap_idles_the_lane_until_the_hour_rolls(settings, helpers):
    clock = helpers.Clock()
    runner = LaneRunner(helpers, auto=True)
    config = lanes(settings, house_concurrent=2, house_max_per_hour=3)
    with TestClient(create_app(config, runner, now=clock)) as client:
        helpers.wait_for(lambda: len(runner.started) == 3)
        helpers.wait_for(lambda: runner.running() == [])
        capped = client.get("/api/admin/house", headers=ADMIN).json()
        assert len(runner.started) == 3
        clock.at += timedelta(hours=1, seconds=1)
        helpers.wait_for(lambda: len(runner.started) == 6)
        client.post("/api/admin/house/pause", headers=ADMIN)

    assert (capped["rate_limited"], capped["started_last_hour"], capped["max_per_hour"]) == (
        True,
        3,
        3,
    )


# ---------- restart recovery ----------


def test_restart_recovery_runs_house_rows_in_the_house_lane(settings, helpers):
    config = lanes(settings, house_concurrent=1, max_concurrent=1)
    store = Store(config.web_db)
    done_id = store.create_house(catalog(), None)[0]
    cut_id, cut_name = store.create_house(catalog(), None)
    for sid in (done_id, cut_id):
        store.mark_running(sid)
    helpers.write_run(
        config.runs_dir, expected_run_dir(config.runs_dir, done_id).name,
        policy_ref="submission-house", period_return="0.0200",
    )  # fmt: skip
    human = store.create(
        name="waiting-human", handle=None, instructions="x" * 20, ip_hash="h",
        max_queue=30, max_per_day=150, max_per_ip_hour=5,
    )  # fmt: skip

    runner = LaneRunner(helpers)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: runner.running() == sorted([cut_name, "waiting-human"]))
        assert status(client, done_id)["status"] == "scored"  # finished on disk: no rerun
        assert status(client, cut_id)["lane"] == "house"
        client.post("/api/admin/house/pause", headers=ADMIN)
        for name in runner.running():
            runner.release(name)
        helpers.wait_for(lambda: status(client, human)["status"] == "scored")

    # The interrupted house run took the one house slot before any new house strategy, and
    # the waiting human had the one human slot to itself.
    assert set(runner.started[:2]) == {cut_name, "waiting-human"}
    assert done_name_not_rerun(runner, store, done_id)


def done_name_not_rerun(runner, store, done_id):
    return store.get(done_id)["name"] not in runner.started


def test_with_the_lane_off_interrupted_house_runs_end_as_failed(settings, helpers):
    store = Store(settings.web_db)
    sid = store.create_house(catalog(), None)[0]
    store.mark_running(sid)
    runner = LaneRunner(helpers, auto=True)
    with TestClient(create_app(lanes(settings, house_enabled=False), runner)) as client:
        row = Store(settings.web_db).get(sid)
        board = client.get("/api/board").json()["rows"]

    assert (row["status"], row["error_class"]) == ("failed", "HouseLaneOff")
    assert runner.started == [] and sid not in {r["id"] for r in board}


# ---------- rows, admin, migration ----------


def test_rows_and_status_carry_the_lane(settings, helpers):
    runner = LaneRunner(helpers)
    with TestClient(create_app(lanes(settings, house_concurrent=1), runner)) as client:
        sid = client.post("/api/submissions", json=body("human-one")).json()["id"]
        helpers.wait_for(lambda: len(runner.running()) == 2)
        rows = client.get("/api/board").json()["rows"]
        human = status(client, sid)
        house_id = next(r["id"] for r in rows if r["lane"] == "house")
        house = status(client, house_id)
        stats = client.get("/api/admin/stats", headers=ADMIN).json()
        client.post("/api/admin/house/pause", headers=ADMIN)
        for name in runner.running():
            runner.release(name)

    assert (human["lane"], house["lane"]) == ("human", "house")
    assert house["position"] is None
    assert {r["lane"] for r in rows if r["status"] == "running"} == {"human", "house"}
    lanes_block = stats["aggregates"]["lanes"]
    assert lanes_block["human"] == {"slots": 3, "running": 1, "queued": 0}
    assert (lanes_block["house"]["slots"], lanes_block["house"]["running"]) == (1, 1)
    assert {s["lane"] for s in stats["submissions"]} == {"human", "house"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/admin/house"), ("post", "/api/admin/house/pause"),
     ("post", "/api/admin/house/resume")],
)  # fmt: skip
def test_house_admin_routes_are_guarded(settings, helpers, method, path):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        call = getattr(client, method)
        assert call(path).status_code == 403
        assert call(path, headers={"X-Bazaar-Admin-Token": "nope"}).status_code == 403
        assert call(path, headers=ADMIN).status_code == 200
    unset = dataclasses.replace(settings, admin_token=None)
    with TestClient(create_app(unset, helpers.FakeRunner())) as client:
        assert getattr(client, method)(path, headers=ADMIN).status_code == 404


def test_migration_adds_lane_and_house_state_to_an_old_database(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, latest_value TEXT, trace_context TEXT, "
            "rerun_of TEXT, error_class TEXT);"
            "CREATE TABLE events (t TEXT NOT NULL, text TEXT NOT NULL, submission_id TEXT);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'queued', '2026-10-08T14:00:00+00:00');"
        )
    conn.close()

    store = Store(path)
    Store(path)  # idempotent

    assert store.get("old1")["lane"] == "human"
    assert store.position("old1") == 1
    assert store.ids_with_status("queued") == ["old1"]
    state = store.house_state()
    assert (state["cursor"], state["cycle"], state["paused"], state["total_started"]) == (
        0,
        1,
        False,
        0,
    )
    with pytest.raises(CapReached):
        store.create(
            name="new-bot", handle=None, instructions="x" * 20, ip_hash="h",
            max_queue=1, max_per_day=150, max_per_ip_hour=5,
        )  # fmt: skip


# ---------- failure backoff ----------


class FailingRunner(LaneRunner):
    """Fails at once while failing is set; otherwise finishes like LaneRunner(auto=True)."""

    def __init__(self, helpers):
        super().__init__(helpers, auto=True)
        self.failing = threading.Event()
        self.failing.set()

    def __call__(self, *, name, **kwargs):
        if self.failing.is_set():
            with self.lock:
                self.started.append(name)
            raise RuntimeError("market down")
        return super().__call__(name=name, **kwargs)


@pytest.fixture
def scaled_backoff(monkeypatch):
    """The backoff at 1:100, so a simulated minute takes 0.6 s: 0.05 s doubling to 3 s."""
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_FAIL_BACKOFF_S", 0.05)
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_FAIL_BACKOFF_MAX_S", 3.0)


def house_rows(path):
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT status FROM submissions WHERE lane = 'house'").fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_a_failing_runner_backs_the_lane_off(settings, helpers, scaled_backoff):
    runner = FailingRunner(helpers)
    config = lanes(settings, house_concurrent=3)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) >= 3)
        time.sleep(0.6)  # a simulated minute
        state = client.get("/api/admin/house", headers=ADMIN).json()
        events = [e["text"] for e in client.get("/api/board").json()["events"]]
        client.post("/api/admin/house/pause", headers=ADMIN)

    rows = house_rows(config.web_db)
    # Without the backoff three slots made hundreds of rows a second; with it, 5 + 10 + 20 + 40
    # + 80 + 160 s of waiting covers the minute after the first failures.
    assert 3 <= len(rows) <= 12 and set(rows) == {"failed"}
    assert state["consecutive_failures"] == len(rows)
    assert state["backoff_until"] is not None
    notices = [e for e in events if e.startswith("house runs paused: runner failing")]
    assert len(notices) == (1 if len(rows) >= 5 else 0)
    assert sum(e.endswith("'s run failed") for e in events) <= 3  # the first ones only


def test_the_backoff_resets_after_a_success(settings, helpers, scaled_backoff):
    runner = FailingRunner(helpers)
    config = lanes(settings, house_concurrent=1)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) >= 3)
        failing = client.get("/api/admin/house", headers=ADMIN).json()
        runner.failing.clear()
        helpers.wait_for(lambda: "scored" in house_rows(config.web_db), timeout=10)
        recovered = client.get("/api/admin/house", headers=ADMIN).json()
        client.post("/api/admin/house/pause", headers=ADMIN)

    assert failing["consecutive_failures"] >= 3 and failing["backoff_until"] is not None
    assert (recovered["consecutive_failures"], recovered["backoff_until"]) == (0, None)


def test_resume_ends_a_backoff(settings, helpers, monkeypatch):
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_FAIL_BACKOFF_S", 60.0)  # far longer than the test
    runner = FailingRunner(helpers)
    config = lanes(settings, house_concurrent=1)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) == 1)
        helpers.wait_for(
            lambda: client.get("/api/admin/house", headers=ADMIN).json()["consecutive_failures"]
        )
        runner.failing.clear()
        resumed = client.post("/api/admin/house/resume", headers=ADMIN).json()
        helpers.wait_for(lambda: "scored" in house_rows(config.web_db))
        client.post("/api/admin/house/pause", headers=ADMIN)

    assert resumed["consecutive_failures"] in (0, 1)  # the reset runs on the loop right after


# ---------- admin rerun ----------


def test_a_house_row_cannot_be_rerun_and_nothing_changes(settings, helpers):
    store = Store(settings.web_db)
    sid, name = store.create_house(catalog(), None)
    store.finish(sid, run_dir=None, error="the run failed; please try again")
    with sqlite3.connect(settings.web_db) as conn:
        before = conn.execute("SELECT * FROM submissions ORDER BY rowid").fetchall()
    conn.close()

    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.post(f"/api/admin/submissions/{sid}/rerun", headers=ADMIN)

    assert (response.status_code, response.json()) == (
        409,
        {"detail": "house runs refill automatically"},
    )
    with sqlite3.connect(settings.web_db) as conn:
        after = conn.execute("SELECT * FROM submissions ORDER BY rowid").fetchall()
    conn.close()
    assert after == before  # no rename, no hide, no new row
    assert Store(settings.web_db).get(sid)["name"] == name


def test_a_human_row_still_reruns(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        sid = client.post("/api/submissions", json=body("human-one")).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        response = client.post(f"/api/admin/submissions/{sid}/rerun", headers=ADMIN)
        new_id = response.json()["id"]
        helpers.wait_for(lambda: status(client, new_id)["status"] == "scored")

    assert response.status_code == 201
    assert Store(seeded.web_db).get(new_id)["lane"] == "human"
