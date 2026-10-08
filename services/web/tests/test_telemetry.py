import dataclasses
import json
import sqlite3
import sys
import types

import logfire
from bazaar_web.app import create_app
from bazaar_web.store import Store
from bazaar_web.worker import created_ns
from fastapi.testclient import TestClient

VALID = {"name": "alice-bot", "handle": None, "instructions": "Buy KO on dips, hold MSFT."}


def spans(capfire):
    return capfire.exporter.exported_spans_as_dict()


def by_name(capfire, name):
    return [s for s in spans(capfire) if s["name"] == name]


def traced_runner(helpers, **kwargs):
    """A fake run_submission that opens a span, like the real runner's runner.run."""
    fake = helpers.FakeRunner(**kwargs)

    def run(**call):
        with logfire.span("runner.run"):
            return fake(**call)

    run.fake = fake
    return run


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


def ancestors(capfire, span):
    """Span ids from span's parent up to its root."""
    index = {s["context"]["span_id"]: s for s in spans(capfire)}
    chain, parent = [], span.get("parent")
    while parent:
        chain.append(parent["span_id"])
        parent = index.get(parent["span_id"], {}).get("parent")
    return chain


def test_the_run_joins_the_trace_of_the_post(seeded, helpers, capfire):
    with TestClient(create_app(seeded, traced_runner(helpers))) as client:
        sid = client.post("/api/submissions", json=VALID).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")

    [post] = by_name(capfire, "POST /api/submissions")
    [queued] = by_name(capfire, "submission queued")
    [run] = by_name(capfire, "runner.run")
    trace_id = post["context"]["trace_id"]
    assert queued["context"]["trace_id"] == run["context"]["trace_id"] == trace_id
    assert run["parent"]["span_id"] == queued["context"]["span_id"]
    assert post["context"]["span_id"] in ancestors(capfire, queued)
    assert queued["attributes"]["submission_id"] == sid
    # The queue wait starts when the submission was created (capfire fakes only its own clock).
    created_at = Store(seeded.web_db).get(sid)["created_at"]
    assert queued["start_time"] == created_ns(created_at)


def test_store_writes_are_traced_inside_the_request(settings, helpers, capfire):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        client.post("/api/submissions", json=VALID)

    [post] = by_name(capfire, "POST /api/submissions")
    inserts = [s for s in spans(capfire) if s["name"] == "INSERT"]
    assert inserts
    assert all(s["context"]["trace_id"] == post["context"]["trace_id"] for s in inserts)


def test_restart_recovery_keeps_the_original_trace(settings, helpers, capfire):
    with logfire.span("original POST") as original:
        carrier = json.dumps(logfire.propagate.get_context())
    store = Store(settings.web_db)
    sid = store.create(
        name="was-running", handle=None, instructions="x" * 20, ip_hash="h",
        max_queue=30, max_per_day=150, max_per_ip_hour=5, trace_context=carrier,
    )  # fmt: skip
    store.mark_running(sid)

    with TestClient(create_app(settings, traced_runner(helpers))) as client:
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")

    trace_id = format(original.get_span_context().trace_id, "032x")
    [run] = by_name(capfire, "runner.run")
    [queued] = by_name(capfire, "submission queued")
    assert format(run["context"]["trace_id"], "032x") == trace_id
    assert format(queued["context"]["trace_id"], "032x") == trace_id


def test_polling_emits_no_spans_at_all(seeded, helpers, capfire):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        sid = client.post("/api/submissions", json=VALID).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        capfire.exporter.clear()
        for _ in range(5):
            assert client.get("/api/board").status_code == 200
            assert client.get(f"/api/submissions/{sid}").status_code == 200
            assert client.get(f"/api/submissions/{sid}?t=1").status_code == 200
            assert client.get("/api/submissions/unknown").status_code == 404
            client.get("/fonts/DwightMedium.woff2")
            assert client.get("/static/vendor/qrcode.min.js").status_code == 200
            client.get("/health")

    assert spans(capfire) == []


def test_posts_admin_routes_and_pages_are_traced(seeded, helpers, capfire):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        sid = client.post("/api/submissions", json=VALID).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        client.post(
            f"/api/admin/submissions/{sid}/hide", headers={"X-Bazaar-Admin-Token": "admin-secret"}
        )
        client.get("/")
        client.get("/submit")

    names = {s["name"] for s in spans(capfire)}
    assert "POST /api/submissions" in names
    assert "POST /api/admin/submissions/{submission_id}/hide" in names
    assert {"GET /", "GET /submit"} <= names
    assert not any("/api/board" in n or n.startswith("GET /api/submissions/") for n in names)


def test_no_secret_reaches_any_span(seeded, helpers, capfire, monkeypatch):
    for name in ("PYDANTIC_AI_GATEWAY_API_KEY", "LOGFIRE_TOKEN"):
        monkeypatch.setenv(name, f"SENTINEL-{name}")
    secret = dataclasses.replace(
        seeded, admin_token="SENTINEL123", runner_token="SENTINEL-RUNNER-TOKEN"
    )
    # The failing run raises outside any runner-style span: the check covers this service's spans.
    # (A runner span that records such an exception would carry its message; see the handoff.)
    with TestClient(create_app(secret, helpers.FakeRunner(fail={"bad-bot"}))) as client:
        headers = {"Authorization": "Bearer SENTINEL-AUTH", "X-Bazaar-Admin-Token": "SENTINEL123"}
        sid = client.post("/api/submissions", json=VALID, headers=headers).json()["id"]
        bad = client.post(
            "/api/submissions",
            json=VALID | {"name": "bad-bot"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        ).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        helpers.wait_for(lambda: status(client, bad)["status"] == "failed")
        hide = f"/api/admin/submissions/{sid}/hide"
        client.post(hide, headers={"X-Bazaar-Admin-Token": "SENTINEL-WRONG"})
        client.post(hide, headers={"X-Bazaar-Admin-Token": "SENTINEL123"})
        client.get("/api/admin/whoami", headers={"X-Bazaar-Admin-Token": "SENTINEL123"})

    dumped = json.dumps(spans(capfire), default=str)
    assert "SENTINEL" not in dumped
    assert "http.request.header" not in dumped
    assert by_name(capfire, "POST /api/admin/submissions/{submission_id}/hide")


OLD_SCHEMAS = {
    # W1 (6bb0763), then ARC2 added latest_value; trace_context comes with T1.
    "w1": "",
    "arc2": ",\n    latest_value TEXT",
}


def test_migration_adds_trace_context_to_older_databases(tmp_path):
    for label, extra in OLD_SCHEMAS.items():
        path = tmp_path / f"{label}.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.executescript(
                "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
                "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
                "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
                f"hidden INTEGER NOT NULL DEFAULT 0{extra});"
                "CREATE TABLE events (t TEXT NOT NULL, text TEXT NOT NULL, submission_id TEXT);"
                "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
                "VALUES ('old1', 'old-bot', 'x', 'h', 'queued', '2026-10-08T14:00:00+00:00');"
            )
        conn.close()

        Store(path)
        store = Store(path)

        columns = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(submissions)")]
        assert columns.count("trace_context") == 1, label
        assert columns.count("latest_value") == 1, label
        row = store.get("old1")
        assert (row["name"], row["status"], row["trace_context"]) == ("old-bot", "queued", None)


def test_configure_names_the_service_bazaar_web(real_configure, monkeypatch):
    calls = {}
    monkeypatch.setattr(logfire, "configure", lambda **kw: calls.update(configure=kw))
    monkeypatch.setattr(logfire, "instrument_system_metrics", lambda: calls.update(metrics=True))
    monkeypatch.setitem(sys.modules, "bazaar_protocol.telemetry", None)  # T0 not landed
    monkeypatch.setenv("BAZAAR_ENVIRONMENT", "production")

    real_configure()

    kwargs = calls["configure"]
    assert kwargs["service_name"] == "bazaar-web"
    assert kwargs["send_to_logfire"] == "if-token-present"
    assert kwargs["environment"] == "production"
    assert kwargs["distributed_tracing"] is True
    assert kwargs["console"] is False
    assert kwargs["scrubbing"].extra_patterns == [r"runner[._ -]?token", r"admin[._ -]?token"]
    assert calls["metrics"] is True


def test_configure_uses_the_shared_helper_once_it_lands(real_configure, monkeypatch):
    calls = []
    shared = types.ModuleType("bazaar_protocol.telemetry")
    shared.configure = calls.append
    monkeypatch.setitem(sys.modules, "bazaar_protocol.telemetry", shared)
    monkeypatch.setattr(logfire, "configure", lambda **kw: calls.append(("fallback", kw)))
    monkeypatch.setattr(logfire, "instrument_system_metrics", lambda: None)

    real_configure()

    assert calls == ["bazaar-web"]
