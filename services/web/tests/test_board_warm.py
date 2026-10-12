"""HF-WARM: /api/board serves its last good payload at once and rebuilds in the background."""

import json
import threading
import time

import bazaar_web.app
import pytest
from bazaar_web.app import create_app
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
        return {"rows": [{"id": f"build-{n}"}], "factory": {"state": "built"}}


@pytest.fixture
def builds(monkeypatch):
    fake = Builds()
    monkeypatch.setattr(bazaar_web.app, "build_board", fake)
    yield fake
    fake.gate.set()


def persisted(settings):
    settings.web_db.parent.mkdir(parents=True, exist_ok=True)
    return settings.web_db.parent / "board-payload.json"


def rows(response):
    return [r["id"] for r in response.json()["rows"]]


def concurrently(client, n):
    out = [None] * n

    def get(i):
        out[i] = client.get("/api/board")

    threads = [threading.Thread(target=get, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    return threads, out


def test_the_persisted_board_is_served_at_once_while_the_rebuild_is_blocked(
    settings, helpers, builds
):
    persisted(settings).write_text(json.dumps({"rows": [{"id": "before-deploy"}]}))
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        for _ in range(5):
            start = time.perf_counter()
            response = client.get("/api/board")
            assert time.perf_counter() - start < 0.5
            assert rows(response) == ["before-deploy"] and response.json()["stale"] is True
        builds.gate.set()  # let the shutdown join the rebuild

    assert builds.calls == 1


def test_concurrent_requests_during_a_blocked_rebuild_start_one_build(settings, helpers, builds):
    persisted(settings).write_text(json.dumps({"rows": [{"id": "before-deploy"}]}))
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        threads, out = concurrently(client, 8)
        for t in threads:
            t.join(5)
        client.app.state.board.invalidate()  # an invalidation joins the running build

        assert builds.calls == 1
        assert all(rows(r) == ["before-deploy"] for r in out)
        builds.gate.set()  # let the shutdown join the rebuild


def test_after_the_rebuild_the_new_payload_is_served_and_persisted(settings, helpers, builds):
    persisted(settings).write_text(json.dumps({"rows": [{"id": "before-deploy"}]}))
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert rows(client.get("/api/board")) == ["before-deploy"]
        builds.gate.set()
        assert client.app.state.board_cache.settled()
        fresh = client.get("/api/board").json()

    assert [r["id"] for r in fresh["rows"]] == ["build-1"] and "stale" not in fresh
    saved = json.loads(persisted(settings).read_text())
    assert saved["rows"] == [{"id": "build-1"}] and "stale" not in saved


def test_the_factory_line_stays_live_on_a_cached_board(settings, helpers, builds):
    persisted(settings).write_text(json.dumps({"rows": [], "factory": {"state": "old"}}))
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        factory = client.get("/api/board").json()["factory"]
        builds.gate.set()  # let the shutdown join the rebuild

    assert factory == {"state": "off", "running": 0}  # the house lane is off in these settings


def test_a_failing_rebuild_keeps_the_last_good_payload(settings, helpers, builds):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        cache = client.app.state.board_cache
        builds.gate.set()
        assert rows(client.get("/api/board")) == ["build-1"]
        builds.fail = True
        cache.invalidate()
        helpers.wait_for(lambda: builds.calls >= 2 and cache._thread is None)
        assert rows(client.get("/api/board")) == ["build-1"]  # the failure is not cached
        builds.fail = False
        last = rows(helpers.board(client))  # the next trigger retries
        assert last == [f"build-{builds.calls}"]

    assert json.loads(persisted(settings).read_text())["rows"] == [{"id": last[0]}]


def test_ttl_expiry_and_invalidate_rebuild_in_the_background(settings, helpers, builds):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        cache = client.app.state.board_cache
        builds.gate.set()
        assert rows(client.get("/api/board")) == ["build-1"]
        assert cache.settled()
        builds.gate.clear()  # the next build blocks

        cache._built_at -= cache.ttl  # the payload is now older than the TTL
        start = time.perf_counter()
        assert rows(client.get("/api/board")) == ["build-1"]  # served, not waited for
        assert time.perf_counter() - start < 0.5
        helpers.wait_for(lambda: builds.calls == 2)  # and one build started behind it

        builds.gate.set()
        assert cache.settled()
        builds.gate.clear()
        client.app.state.board.invalidate()  # a scored, hidden or rerun row
        start = time.perf_counter()
        assert rows(client.get("/api/board")) == ["build-2"]
        assert time.perf_counter() - start < 0.5
        helpers.wait_for(lambda: builds.calls == 3)
        builds.gate.set()


def test_a_first_boot_with_nothing_persisted_shares_one_build(settings, helpers, builds):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        threads, out = concurrently(client, 5)
        time.sleep(0.3)
        assert all(t.is_alive() for t in threads)  # the one wait: nothing to serve yet
        assert builds.calls == 1  # sharing the startup build, never starting a second
        builds.gate.set()
        for t in threads:
            t.join(5)

    assert all(rows(r) == ["build-1"] for r in out)


def test_a_first_boot_whose_build_fails_answers_503(settings, helpers, builds):
    builds.fail = True
    builds.gate.set()
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.get("/api/board")

    assert response.status_code == 503


def test_an_unreadable_persisted_board_is_ignored(settings, helpers, builds):
    persisted(settings).write_text("{not json")
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        threads, out = concurrently(client, 1)
        time.sleep(0.3)
        assert threads[0].is_alive()  # nothing to serve: it waits for the startup build
        builds.gate.set()
        threads[0].join(5)

    assert rows(out[0]) == ["build-1"]
