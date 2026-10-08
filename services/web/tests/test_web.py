import dataclasses
import sys
from datetime import timedelta

import bazaar_web.app
import pytest
from bazaar_web.app import create_app
from bazaar_web.store import Store
from bazaar_web.worker import expected_run_dir
from fastapi.testclient import TestClient

VALID = {"name": "alice-bot", "handle": "@alice", "instructions": "Buy KO on dips, hold MSFT."}


def submit(client, ip="10.0.0.1", **body):
    return client.post(
        "/api/submissions", json=VALID | body, headers={"X-Forwarded-For": f"203.0.113.9, {ip}"}
    )


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


def rows(client):
    return client.get("/api/board").json()["rows"]


def test_submit_returns_queued_with_position(settings, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    with TestClient(create_app(settings, runner)) as client:
        first = submit(client, name="first-bot", ip="10.0.0.1")
        helpers.wait_for(lambda: status(client, first.json()["id"])["status"] == "running")
        for i in range(3):
            submit(client, name=f"more-{i}", ip=f"10.0.1.{i}")
        last = submit(client, name="last-bot", ip="10.0.0.9")
        runner.gate.set()

    assert first.status_code == 201
    assert first.json()["status"] == "queued"
    assert first.json()["position"] == 1
    assert last.json()["position"] == 2  # three running, one queued ahead of it


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Bad Name"},
        {"name": "ab"},
        {"name": "x" * 41},
        {"instructions": "too short"},
        {"instructions": "  " + "x" * 19 + "  "},
        {"instructions": "x" * 4001},
        {"handle": "h" * 41},
        {"extra": "field"},
    ],
)
def test_invalid_submissions_are_422(settings, helpers, body):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert submit(client, **body).status_code == 422


def test_duplicate_name_is_422(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        submit(client)
        response = submit(client, ip="10.9.9.9")

    assert response.status_code == 422
    assert response.json() == {"detail": "that name is taken"}


def test_blank_handle_becomes_null(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        submission_id = submit(client, handle="   ").json()["id"]
        handle = Store(settings.web_db).get(submission_id)["handle"]

    assert handle is None


def test_queue_cap(settings, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    capped = dataclasses.replace(settings, max_queue=2)
    with TestClient(create_app(capped, runner)) as client:
        submit(client, name="one-bot", ip="10.0.0.1")
        submit(client, name="two-bot", ip="10.0.0.2")
        response = submit(client, name="three-bot", ip="10.0.0.3")
        runner.gate.set()

    assert response.status_code == 429
    assert "queue is full" in response.json()["detail"]


def test_daily_cap_resets_at_utc_midnight(settings, helpers):
    clock = helpers.Clock()
    capped = dataclasses.replace(settings, max_per_day=2)
    with TestClient(create_app(capped, helpers.FakeRunner(), now=clock)) as client:
        submit(client, name="one-bot", ip="10.0.0.1")
        submit(client, name="two-bot", ip="10.0.0.2")
        blocked = submit(client, name="three-bot", ip="10.0.0.3")
        clock.at += timedelta(days=1)
        allowed = submit(client, name="four-bot", ip="10.0.0.4")

    assert blocked.status_code == 429
    assert "limit" in blocked.json()["detail"]
    assert allowed.status_code == 201


def test_ip_cap_uses_rightmost_forwarded_entry_over_a_rolling_hour(settings, helpers):
    clock = helpers.Clock()
    capped = dataclasses.replace(settings, max_per_ip_hour=2)
    with TestClient(create_app(capped, helpers.FakeRunner(), now=clock)) as client:

        def post(name, forwarded):
            body = VALID | {"name": name}
            return client.post(
                "/api/submissions", json=body, headers={"X-Forwarded-For": forwarded}
            )

        post("one-bot", "1.1.1.1, 9.9.9.9")
        post("two-bot", "2.2.2.2, 9.9.9.9")
        spoofed = post("three-bot", "3.3.3.3, 9.9.9.9")
        other = post("four-bot", "9.9.9.9, 5.5.5.5")
        clock.at += timedelta(minutes=61)
        later = post("five-bot", "9.9.9.9")

    assert spoofed.status_code == 429
    assert "network" in spoofed.json()["detail"]
    assert other.status_code == 201
    assert later.status_code == 201


def test_raw_ip_is_never_stored(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        submit(client, ip="198.51.100.77")

    assert b"198.51.100.77" not in settings.web_db.read_bytes()


def test_submission_runs_to_scored_and_ranks_on_the_board(seeded, helpers):
    runner = helpers.FakeRunner(returns={"alice-bot": "0.0450"})
    runner.gate.clear()
    with TestClient(create_app(seeded, runner)) as client:
        submission_id = submit(client).json()["id"]
        running = helpers.wait_for(lambda: (s := status(client, submission_id))["day"] == 10 and s)
        board_running = {r["id"]: r for r in rows(client)}[submission_id]
        runner.gate.set()
        done = helpers.wait_for(
            lambda: (s := status(client, submission_id))["status"] == "scored" and s
        )
        board = client.get("/api/board").json()

    assert running["status"] == "running"
    assert running["position"] is None
    assert board_running["status"] == "running"
    assert board_running["return_pct"] is None
    assert done["return_pct"] == 4.5
    assert done["rank"] == 1
    assert done["error"] is None
    top = board["rows"][0]
    assert (top["id"], top["name"], top["handle"], top["rank"]) == (
        submission_id,
        "alice-bot",
        "@alice",
        1,
    )
    assert top["excess_pct"] == 1.4
    assert top["history"] == [0.0, 1.2]
    assert any("alice-bot finished at +4.50%" == e["text"] for e in board["events"])
    day_events = [e for e in board["events"] if "day" in e["text"]]
    assert len(day_events) == 10


def test_percent_is_applied_exactly_once(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        agent = {r["id"]: r for r in rows(client)}["seed-agent"]

    assert agent["return_pct"] == 0.6  # evaluator fraction 0.006011
    assert agent["history"] == [0.0, 0.26, 0.6]
    assert agent["excess_pct"] == -2.5  # 0.006011 - 0.0310
    assert agent["fills"] == 3
    assert agent["trace_id"] == "0af7651916cd43dd8448eb211c80319c"


def test_failed_run_reports_a_generic_error(seeded, helpers, caplog, monkeypatch):
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "SENTINEL-GATEWAY-KEY-7f3a")
    monkeypatch.setenv("LOGFIRE_TOKEN", "")
    runner = helpers.FakeRunner(fail={"alice-bot"})
    with TestClient(create_app(seeded, runner)) as client:
        submission_id = submit(client).json()["id"]
        done = helpers.wait_for(
            lambda: (s := status(client, submission_id))["status"] == "failed" and s
        )
        board_text = client.get("/api/board").text

    assert done["error"] == "the run failed; please try again"
    assert done["rank"] is None
    assert "market said no" not in board_text
    assert "super-secret-token" not in board_text
    assert "RuntimeError: market said no: token=*** key=***" in caplog.text
    assert "super-secret-token" not in caplog.text
    assert "SENTINEL-GATEWAY-KEY-7f3a" not in caplog.text
    assert "SENTINEL-GATEWAY-KEY-7f3a" not in board_text
    assert submission_id not in [r["id"] for r in rows_from(board_text)]


def rows_from(text):
    import json

    return json.loads(text)["rows"]


def test_missing_runner_fails_with_runner_unavailable(settings, monkeypatch, helpers):
    monkeypatch.setitem(sys.modules, "bazaar_runner.submission", None)
    with TestClient(create_app(settings)) as client:
        submission_id = submit(client).json()["id"]
        done = helpers.wait_for(
            lambda: (s := status(client, submission_id))["status"] == "failed" and s
        )

    assert done["error"] == "runner unavailable"


def test_at_most_three_run_at_once(settings, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    with TestClient(create_app(settings, runner)) as client:
        ids = [submit(client, name=f"bot-{i}", ip=f"10.0.0.{i}").json()["id"] for i in range(4)]
        helpers.wait_for(lambda: [status(client, i)["status"] for i in ids[:3]] == ["running"] * 3)
        fourth = status(client, ids[3])
        runner.gate.set()
        helpers.wait_for(lambda: all(status(client, i)["status"] == "scored" for i in ids))

    assert fourth["status"] == "queued"
    assert fourth["position"] == 1
    assert len(runner.calls) == 4
    assert all(call["runner_token"] == "super-secret-token" for call in runner.calls)


def stored(settings, *names, running=()):
    """Create submissions directly in the store, as a previous process would have."""
    store = Store(settings.web_db)
    common = {"handle": None, "ip_hash": "x", "max_queue": 30, "max_per_day": 150}
    ids = {
        name: store.create(name=name, instructions="x" * 20, max_per_ip_hour=50, **common)
        for name in names
    }
    for name in running:
        store.mark_running(ids[name])
    return ids


def test_expected_run_dir_matches_the_runner_formula(tmp_path):
    # uuid5(uuid5(NAMESPACE_URL, "bazaar:sub-<id>"), "run"), computed once by hand.
    run_dir = expected_run_dir(tmp_path, "0123456789abcdef0123456789abcdef")

    assert run_dir == tmp_path / "023a6981-d929-5ea8-9056-171cd1800d9e"


def test_restart_scores_a_run_that_finished_on_disk(seeded, helpers):
    ids = stored(seeded, "was-running", running=["was-running"])
    run_dir = expected_run_dir(seeded.runs_dir, ids["was-running"])
    helpers.write_run(
        seeded.runs_dir, run_dir.name, policy_ref="submission-was-running", period_return="0.0450"
    )
    runner, calls = helpers.FakeRunner(), []
    app = create_app(seeded, runner, on_scored=lambda sid, path: calls.append((sid, path)))
    with TestClient(app) as client:
        done = status(client, ids["was-running"])
        row = {r["id"]: r for r in rows(client)}[ids["was-running"]]
        events = client.get("/api/board").json()["events"]

    assert (done["status"], done["rank"], done["return_pct"]) == ("scored", 1, 4.5)
    assert (row["name"], row["status"]) == ("was-running", "scored")
    assert runner.calls == []
    assert calls == [(ids["was-running"], run_dir)]
    assert events[0]["text"] == "was-running finished at +4.50%"


def test_restart_requeues_an_unfinished_run_ahead_of_waiting_ones(settings, helpers):
    ids = stored(settings, "was-queued", "was-running", running=["was-running"])
    runner = helpers.FakeRunner()
    runner.gate.clear()
    with TestClient(create_app(dataclasses.replace(settings, max_concurrent=1), runner)) as client:
        helpers.wait_for(lambda: runner.calls)
        first = runner.calls[0]["name"]
        waiting = status(client, ids["was-queued"])
        runner.gate.set()
        helpers.wait_for(lambda: all(status(client, i)["status"] == "scored" for i in ids.values()))

    assert first == "was-running"  # it had already started, so it goes first
    assert waiting["status"] == "queued"
    assert [c["name"] for c in runner.calls] == ["was-running", "was-queued"]


def test_restart_ignores_staging_dirs(settings, helpers):
    ids = stored(settings, "was-running", running=["was-running"])
    staging = settings.runs_dir / ".tmp-0123abcd"
    helpers.write_run(settings.runs_dir, staging.name, policy_ref="x", period_return="0.01")
    runner = helpers.FakeRunner()
    with TestClient(create_app(settings, runner)) as client:
        helpers.wait_for(lambda: status(client, ids["was-running"])["status"] == "scored")

    assert [c["name"] for c in runner.calls] == ["was-running"]


def test_board_order_and_ranks(seeded, helpers):
    runner = helpers.FakeRunner(returns={"low-bot": "-0.0200"}, fail={"bad-bot"})
    with TestClient(create_app(seeded, runner)) as client:
        low = submit(client, name="low-bot", ip="10.0.0.1").json()["id"]
        helpers.wait_for(lambda: status(client, low)["status"] == "scored")
        bad = submit(client, name="bad-bot", ip="10.0.0.2").json()["id"]
        helpers.wait_for(lambda: status(client, bad)["status"] == "failed")
        runner.gate.clear()
        ids = [submit(client, name=f"wait-{i}", ip=f"10.0.1.{i}").json()["id"] for i in range(4)]
        helpers.wait_for(lambda: [status(client, i)["status"] for i in ids[:3]] == ["running"] * 3)
        board = rows(client)
        runner.gate.set()

    summary = [(r["name"], r["status"], r["rank"]) for r in board]
    assert summary[:4] == [
        ("baseline-buy-and-hold", "scored", 1),
        ("scripted-momentum-v1", "scored", 2),
        ("baseline-cash-only", "scored", 3),
        ("low-bot", "scored", 4),
    ]
    assert [s for _, s, _ in summary[4:]] == ["running"] * 3 + ["queued", "failed"]
    assert all(rank is None for _, _, rank in summary[4:])
    refused = board[-1]
    assert (refused["id"], refused["return_pct"], refused["fills"]) == ("seed-refused", None, None)
    assert "bad-bot" not in [r["name"] for r in board]  # failed before any run dir existed


def test_hide_requires_the_admin_token(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        submission_id = submit(client, name="rude-name").json()["id"]
        helpers.wait_for(lambda: status(client, submission_id)["status"] == "scored")
        url = f"/api/admin/submissions/{submission_id}/hide"
        missing = client.post(url)
        wrong = client.post(url, headers={"X-Bazaar-Admin-Token": "nope"})
        ok = client.post(url, headers={"X-Bazaar-Admin-Token": "admin-secret"})
        unknown = client.post(
            "/api/admin/submissions/nope/hide", headers={"X-Bazaar-Admin-Token": "admin-secret"}
        )
        board = client.get("/api/board").json()
        after = client.get(f"/api/submissions/{submission_id}")

    assert (missing.status_code, wrong.status_code, ok.status_code) == (403, 403, 204)
    assert unknown.status_code == 404
    assert "rude-name" not in str(board)
    assert after.status_code == 404


def test_hide_is_disabled_without_an_admin_token(settings, helpers):
    open_settings = dataclasses.replace(settings, admin_token=None)
    with TestClient(create_app(open_settings, helpers.FakeRunner())) as client:
        submission_id = submit(client).json()["id"]
        response = client.post(
            f"/api/admin/submissions/{submission_id}/hide",
            headers={"X-Bazaar-Admin-Token": "anything"},
        )

    assert response.status_code == 404


def test_board_survives_a_new_app_instance(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        submission_id = submit(client).json()["id"]
        helpers.wait_for(lambda: status(client, submission_id)["status"] == "scored")

    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        row = {r["id"]: r for r in rows(client)}[submission_id]

    assert (row["name"], row["status"], row["return_pct"]) == ("alice-bot", "scored", 1.2)


def test_on_scored_hook_runs_once_per_scored_run(seeded, helpers):
    calls = []
    runner = helpers.FakeRunner(fail={"bad-bot"})
    app = create_app(seeded, runner)
    app.state.on_scored = lambda submission_id, run_dir: calls.append((submission_id, run_dir))
    with TestClient(app) as client:
        good = submit(client, name="good-bot", ip="10.0.0.1").json()["id"]
        bad = submit(client, name="bad-bot", ip="10.0.0.2").json()["id"]
        helpers.wait_for(lambda: status(client, good)["status"] == "scored")
        helpers.wait_for(lambda: status(client, bad)["status"] == "failed")
        helpers.wait_for(lambda: calls)

    assert [(sid, path.name) for sid, path in calls] == [(good, f"sub-{good}")]


def test_raising_on_scored_hook_leaves_the_run_scored(seeded, helpers, caplog):
    def hook(submission_id, run_dir):
        raise OSError("volume full")

    with TestClient(create_app(seeded, helpers.FakeRunner(), on_scored=hook)) as client:
        submission_id = submit(client).json()["id"]
        helpers.wait_for(lambda: "on_scored hook failed" in caplog.text)
        done = status(client, submission_id)

    assert done["status"] == "scored"
    assert done["rank"] is not None


def test_unknown_submission_is_404(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert client.get("/api/submissions/nope").status_code == 404


def test_pages_fall_back_to_placeholders(settings, helpers, tmp_path, monkeypatch):
    # An empty static dir: the real board.html and submit.html (evals team) must not leak in.
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(bazaar_web.app, "STATIC", static)
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        board, form = client.get("/"), client.get("/submit")

    assert board.status_code == form.status_code == 200
    assert "The live board is coming soon." in board.text
    assert "The submission form is coming soon." in form.text


def test_empty_runs_dir_gives_default_window(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        payload = client.get("/api/board").json()

    assert payload["window"] == {
        "start": "2026-02-02",
        "end": "2026-02-13",
        "starting_cash": 10000.0,
        "symbols": ["AAPL", "MSFT", "KO"],
        "days": 10,
    }
    assert payload["rows"] == []


def test_numeric_fields_are_json_numbers(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        submission_id = submit(client).json()["id"]
        helpers.wait_for(lambda: status(client, submission_id)["status"] == "scored")
        payload = client.get("/api/board").json()
        done = status(client, submission_id)

    assert isinstance(payload["window"]["starting_cash"], float)
    for row in payload["rows"]:
        for field in ("return_pct", "excess_pct"):
            assert row[field] is None or isinstance(row[field], float), (field, row)
        assert row["fills"] is None or isinstance(row["fills"], int)
        assert row["rank"] is None or isinstance(row["rank"], int)
        assert row["history"] is None or all(isinstance(v, float) for v in row["history"])
    assert isinstance(done["return_pct"], float)
    assert isinstance(done["rank"], int)


def test_admin_whoami_shows_the_cap_ip(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        headers = {"X-Forwarded-For": "198.51.100.1, 203.0.113.7"}
        denied = client.get("/api/admin/whoami", headers=headers)
        seen = client.get(
            "/api/admin/whoami", headers=headers | {"X-Bazaar-Admin-Token": "admin-secret"}
        )

    assert denied.status_code == 403
    assert seen.json() == {
        "x_forwarded_for": "198.51.100.1, 203.0.113.7",
        "peer": "testclient",
        "cap_ip": "203.0.113.7",
    }
