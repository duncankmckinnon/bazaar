"""HF-WARM: /api/board serves its last good payload and rebuilds in the background; a state
change shows on the next poll (bounded wait), TTL expiry never waits."""

import dataclasses
import json
import sqlite3
import threading
import time

import bazaar_web.app
import pytest
from bazaar_web.app import create_app
from bazaar_web.store import Store
from fastapi.testclient import TestClient


class Builds:
    """build_board as an event-gated fake: each call waits for the gate, then returns build n."""

    def __init__(self):
        self.calls = 0
        self.gate = threading.Event()
        self.fail = False

    def __call__(self, *args, **kwargs):
        self.calls += 1
        n = self.calls
        assert self.gate.wait(10)
        if self.fail:
            raise RuntimeError("market down")
        return {"rows": [{"id": f"build-{n}"}], "window": {"starting_cash": 10000.0}}


@pytest.fixture
def builds(monkeypatch):
    fake = Builds()
    monkeypatch.setattr(bazaar_web.app, "build_board", fake)
    yield fake
    fake.gate.set()


def persist(settings, payload):
    settings.web_db.parent.mkdir(parents=True, exist_ok=True)
    (settings.web_db.parent / "board-payload.json").write_text(json.dumps(payload))


def persisted(settings):
    return json.loads((settings.web_db.parent / "board-payload.json").read_text())


def ids(response):
    return [r["id"] for r in response.json()["rows"]]


def timed_get(client):
    start = time.perf_counter()
    response = client.get("/api/board")
    return response, time.perf_counter() - start


def settle(client, builds):
    """Open the gate and wait out the boot builds (a startup write chains a second one); the
    board then and the build count."""
    builds.gate.set()
    assert client.app.state.board_cache.settled()
    return ids(client.get("/api/board")), builds.calls


def concurrently(client, n):
    out = [None] * n

    def get(i):
        out[i] = client.get("/api/board")

    threads = [threading.Thread(target=get, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    return threads, out


def test_the_persisted_board_is_served_at_once_while_the_boot_build_is_blocked(
    settings, helpers, builds
):
    persist(settings, {"rows": [{"id": "before-deploy"}]})
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        for _ in range(5):
            response, took = timed_get(client)
            assert took < 0.5
            assert ids(response) == ["before-deploy"] and response.json()["stale"] is True
        threads, out = concurrently(client, 8)
        for t in threads:
            t.join(5)
        assert builds.calls == 1  # the boot build only: no request started another
        builds.gate.set()

    assert all(ids(r) == ["before-deploy"] for r in out)


def test_after_the_build_the_new_payload_is_served_and_persisted(settings, helpers, builds):
    persist(settings, {"rows": [{"id": "before-deploy"}]})
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert ids(client.get("/api/board")) == ["before-deploy"]
        builds.gate.set()
        assert client.app.state.board_cache.settled()
        fresh = client.get("/api/board").json()

    assert [r["id"] for r in fresh["rows"]] == ["build-1"] and "stale" not in fresh
    assert persisted(settings)["rows"] == [{"id": "build-1"}] and "stale" not in persisted(settings)


def test_an_invalidation_waits_briefly_then_serves_the_previous_board(settings, helpers, builds):
    quick = dataclasses.replace(settings, board_fresh_wait_ms=300)
    with TestClient(create_app(quick, helpers.FakeRunner())) as client:
        before, n = settle(client, builds)
        builds.gate.clear()  # the next build blocks
        client.app.state.board_cache.invalidate()
        response, took = timed_get(client)
        assert ids(response) == before and 0.25 < took < 1.5  # waited, then the old board
        builds.gate.set()
        assert ids(client.get("/api/board")) == [f"build-{n + 1}"]  # the next poll: the new one


def test_a_state_change_shows_on_the_next_poll(settings, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()  # the submission stays queued or running
    with TestClient(create_app(settings, runner)) as client:
        client.get("/api/board")
        sid = client.post(
            "/api/submissions",
            json={"name": "next-poll", "instructions": "Buy KO on dips, hold MSFT.",
                  "email": "a@example.com"},
        ).json()["id"]  # fmt: skip
        shown = ids(client.get("/api/board"))  # no settle, no explicit invalidate
        runner.gate.set()

    assert sid in shown


def test_ttl_expiry_rebuilds_in_the_background_without_waiting(settings, helpers, builds):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        before, n = settle(client, builds)
        cache = client.app.state.board_cache
        builds.gate.clear()
        cache._built_at -= cache.ttl  # older than the TTL, no state change
        response, took = timed_get(client)
        assert ids(response) == before and took < 0.5
        helpers.wait_for(lambda: builds.calls == n + 1)  # one build started behind it
        builds.gate.set()


def test_a_failing_rebuild_keeps_the_last_good_payload(settings, helpers, builds):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        cache = client.app.state.board_cache
        before, n = settle(client, builds)
        builds.fail = True
        cache.invalidate()
        assert ids(client.get("/api/board")) == before  # failed: not cached, not waited on
        builds.fail = False
        cache.invalidate()  # the next trigger retries
        last = ids(client.get("/api/board"))

    assert last == [f"build-{builds.calls}"] and builds.calls >= n + 2
    assert persisted(settings)["rows"] == [{"id": last[0]}]


def test_a_first_boot_with_nothing_persisted_shares_one_build(settings, helpers, builds):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        threads, out = concurrently(client, 5)
        time.sleep(0.3)
        assert all(t.is_alive() for t in threads)  # the one unbounded wait: nothing to serve
        assert builds.calls == 1
        builds.gate.set()
        for t in threads:
            t.join(5)

    assert all(ids(r) == ["build-1"] for r in out)


def test_a_first_boot_whose_build_fails_answers_503(settings, helpers, builds):
    builds.fail = True
    builds.gate.set()
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.get("/api/board")

    assert response.status_code == 503


def test_an_unreadable_persisted_board_is_ignored(settings, helpers, builds):
    persist(settings, {})
    (settings.web_db.parent / "board-payload.json").write_text("{not json")
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        threads, out = concurrently(client, 1)
        time.sleep(0.3)
        assert threads[0].is_alive()  # nothing to serve: it waits for the boot build
        builds.gate.set()
        threads[0].join(5)

    assert ids(out[0]) == ["build-1"]


def test_the_factory_line_and_window_stay_live_on_a_cached_board(settings, helpers, builds):
    persist(settings, {"rows": [], "factory": {"state": "old"}, "window": {"start": "old"}})
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        body = client.get("/api/board").json()
        builds.gate.set()

    assert body["factory"] == {"state": "off", "running": 0}  # the house lane is off here
    assert body["window"]["start"] != "old" and body["window"]["starting_cash"] == 10000.0


def test_a_hidden_row_is_gone_on_the_next_poll(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        sid = client.post(
            "/api/submissions",
            json={"name": "takedown", "instructions": "Buy KO on dips, hold MSFT.",
                  "email": "a@example.com"},
        ).json()["id"]  # fmt: skip
        helpers.wait_for(lambda: client.get(f"/api/submissions/{sid}").json()["status"] == "scored")
        assert sid in ids(client.get("/api/board"))
        hidden = client.post(
            f"/api/admin/submissions/{sid}/hide", headers={"X-Bazaar-Admin-Token": "admin-secret"}
        )
        after = ids(client.get("/api/board"))  # no settle, no explicit invalidate

    assert hidden.status_code == 204 and sid not in after


def test_a_persisted_board_never_shows_a_row_hidden_since(settings, helpers, builds):
    store = Store(settings.web_db)
    sid = store.create(
        name="taken-down", handle=None, instructions="Buy KO on dips, hold MSFT.", ip_hash="h",
        max_queue=30, max_per_day=150, max_per_ip_hour=5,
    )  # fmt: skip
    with sqlite3.connect(settings.web_db) as conn:
        conn.execute("UPDATE submissions SET status = 'scored', hidden = 1 WHERE id = ?", (sid,))
    conn.close()
    persist(settings, {"rows": [{"id": sid}, {"id": "kept"}]})
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        during_boot = ids(client.get("/api/board"))  # the boot build is still blocked
        builds.gate.set()

    assert during_boot == ["kept"]
