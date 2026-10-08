"""Market telemetry: which requests make spans, how many SQLite spans a run makes, and that no
header secret reaches a span."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market import app as app_module
from bazaar_market.app import create_app
from bazaar_market.ledger_api import ApprovalId
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from fastapi.testclient import TestClient

TOKEN = "runner-token-SENTINEL-7f3a"
APPROVAL = UUID("5e471e1e-0000-4000-8000-5e471e1e5e47")
START = date(2025, 7, 1)
SESSIONS = [d for d in (START + timedelta(days=i) for i in range(30)) if d.weekday() < 5][:10]


class AllowOne:
    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return approval_id == APPROVAL


@pytest.fixture
def client(tmp_path, monkeypatch, capfire):
    monkeypatch.setattr(app_module, "configure_telemetry", lambda: None)  # keep capfire's config
    app_module.attach_log_handlers()
    path = seed(tmp_path)
    with TestClient(create_app(path, AllowOne(), runner_token=TOKEN)) as client:
        capfire.exporter.clear()
        yield client


def seed(tmp_path):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        bars = [
            Bar(symbol=symbol, session=day, open=Decimal(price), high=Decimal(price),
                low=Decimal(price), close=Decimal(price), volume=1000)
            for symbol, price in (("AAPL", "100.00"), ("KO", "60.00"))
            for day in SESSIONS
        ]  # fmt: skip
        import_bars(connection, bars, data_version="test-v1", source="synthetic")
    return path


def headers() -> dict[str, str]:
    return {"X-Bazaar-Approval": str(APPROVAL), "X-Bazaar-Runner-Token": TOKEN}


def spans(capfire) -> list[dict]:
    return capfire.exporter.exported_spans_as_dict()


def db_spans(capfire) -> list[dict]:
    return [s for s in spans(capfire) if s["name"] == "ledger db {kind}"]


def demo_run(client, capfire) -> tuple[dict[str, list[int]], str]:
    """One run: per session a cutoff, then on alternate days an order, then a portfolio read.
    Returns the ledger db span count of each request, by kind, and the account route."""
    eid = uuid4()
    counts: dict[str, list[int]] = {"cutoff": [], "create": [], "order": [], "portfolio": []}

    def measured(kind, method, url, **kwargs):
        before = len(db_spans(capfire))
        response = client.request(method, url, headers=headers(), **kwargs)
        assert response.status_code in (200, 201), response.text
        counts[kind].append(len(db_spans(capfire)) - before)
        return response

    base = ""
    for i, day in enumerate(SESSIONS):
        body = {"cutoff": close_at(day).isoformat()}
        if i == 0:
            body |= {"data_version": "test-v1", "execution_rule_version": "exec-v1"}
        measured("cutoff", "PUT", f"/experiments/{eid}/cutoff", json=body)
        if i == 0:
            account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
                       "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip
            created = measured("create", "POST", f"/experiments/{eid}/accounts", json=account)
            base = f"/experiments/{eid}/accounts/{created.json()['account_id']}"
        if i % 2 == 0:
            order = {"client_order_id": str(uuid4()), "symbol": "AAPL",
                     "side": "buy" if i < 6 else "sell", "quantity": "1"}  # fmt: skip
            measured("order", "POST", f"{base}/orders", json=order)
        measured("portfolio", "GET", f"{base}/portfolio")
    return counts, base


def statement_spans(capfire) -> list[dict]:
    return [s for s in spans(capfire) if s["attributes"].get("db.system") == "sqlite"]


def test_each_ledger_transaction_has_one_span_with_its_statements_inside(client, capfire):
    counts, _ = demo_run(client, capfire)
    assert counts == {"cutoff": [0] * 10, "create": [1], "order": [1] * 5, "portfolio": [1] * 10}
    assert len(db_spans(capfire)) == 16
    statements = statement_spans(capfire)
    transactions = {s["context"]["span_id"]: s for s in db_spans(capfire)}
    assert all(s["parent"]["span_id"] in transactions for s in statements)
    assert sum(t["attributes"]["statement_count"] for t in transactions.values()) == len(statements)
    per_order = [
        t["attributes"]["statement_count"]
        for t in transactions.values()
        if t["attributes"]["operation"] == "submit"
    ]
    print(f"\nstatement spans per run: {len(statements)}; per order: {per_order}")
    writes = [s for s in db_spans(capfire) if s["attributes"]["operation"] == "submit"]
    assert len(writes) == 5 and {s["attributes"]["kind"] for s in writes} == {"write"}
    assert all(s["attributes"]["statement_count"] > 0 for s in db_spans(capfire))
    for span in db_spans(capfire):
        user_keys = {k for k in span["attributes"] if not k.startswith(("logfire.", "code."))}
        assert user_keys == {"kind", "operation", "statement_count"}, user_keys


def test_an_order_db_span_nests_under_the_order_and_request(client, capfire):
    demo_run(client, capfire)
    by_id = {s["context"]["span_id"]: s for s in spans(capfire)}
    write = [s for s in db_spans(capfire) if s["attributes"]["operation"] == "submit"][-1]
    chain = []
    span = write
    while span["parent"] is not None and span["parent"]["span_id"] in by_id:
        span = by_id[span["parent"]["span_id"]]
        chain.append(span["name"])
    assert chain[0] == "order {side} {quantity} {symbol}"
    assert "POST /experiments/{experiment_id}/accounts/{account_id}/orders" in chain


def test_health_makes_no_span_and_an_order_route_does(client, capfire):
    assert client.get("/health").status_code == 200
    assert not [s for s in spans(capfire) if "health" in s["name"]]
    demo_run(client, capfire)
    names = {s["name"] for s in spans(capfire)}
    assert "POST /experiments/{experiment_id}/accounts/{account_id}/orders" in names
    assert not [s for s in spans(capfire) if "health" in s["name"]]


def test_a_caller_traceparent_parents_the_market_request(client, capfire):
    trace_id, parent_id = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    response = client.get(
        f"/experiments/{uuid4()}/cutoff/nothing",
        headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
    )
    assert response.status_code == 404
    roots = [s for s in spans(capfire) if s["parent"] and s["parent"]["is_remote"]]
    assert roots, [s["name"] for s in spans(capfire)]
    assert {s["context"]["trace_id"] for s in roots} == {int(trace_id, 16)}
    assert {s["parent"]["span_id"] for s in roots} == {int(parent_id, 16)}


def test_no_span_or_log_carries_a_credential_or_a_bound_value(tmp_path, monkeypatch, capfire):
    """Through the default SqliteGrants path: grant, cutoff, account and order, with statement
    spans on. Fails if a future logfire or OTel version starts recording parameter values."""
    monkeypatch.setattr(app_module, "configure_telemetry", lambda: None)
    app_module.attach_log_handlers()
    experiment = uuid4()
    bound_only = "b0b0b0b0-5e47-4e47-8e47-5e471e1e0001"  # request_id: a SQL parameter, never logged
    with TestClient(create_app(seed(tmp_path), runner_token=TOKEN)) as client:
        capfire.exporter.clear()
        granted = client.post(
            "/control/grants",
            json={"approval_id": str(APPROVAL), "experiment_id": str(experiment)},
            headers={"X-Bazaar-Runner-Token": TOKEN},
        )
        assert granted.status_code == 204
        body = {"cutoff": close_at(SESSIONS[0]).isoformat(), "data_version": "test-v1",
                "execution_rule_version": "exec-v1"}  # fmt: skip
        url = f"/experiments/{experiment}"
        assert client.put(f"{url}/cutoff", json=body, headers=headers()).status_code == 200
        account = {"request_id": bound_only, "agent_id": str(uuid4()),
                   "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip
        created = client.post(f"{url}/accounts", json=account, headers=headers())
        order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy",
                 "quantity": "1"}  # fmt: skip
        placed = client.post(
            f"{url}/accounts/{created.json()['account_id']}/orders", json=order, headers=headers()
        )
        assert placed.json()["status"] == "filled"

    everything = spans(capfire)
    assert statement_spans(capfire), "statement spans must be on for this test to mean anything"
    for span in everything:
        text = repr(span)
        assert TOKEN not in text, span["name"]
        assert str(APPROVAL) not in text, span["name"]
        assert bound_only not in text, span["name"]
        assert not [k for k in span["attributes"] if k.startswith("db.statement.parameters")]
    # The approval logs did reach Logfire, carrying only the ref.
    ref = ApprovalId(APPROVAL).ref
    logged = [s for s in everything if s["name"].startswith(("approval allowed", "grant created"))]
    assert logged and all(ref in repr(s) for s in logged)


def test_failure_paths_leak_no_credential_into_spans(tmp_path, monkeypatch, capfire):
    """Exception text and stack traces are exported verbatim, so drive the failure paths with
    sentinel credentials and check every attribute and event, exceptions included."""
    monkeypatch.setattr(app_module, "configure_telemetry", lambda: None)
    app_module.attach_log_handlers()
    malformed = "approval-SENTINEL-malformed-6d2f"
    unknown = "a11c0de5-5e47-4e47-8e47-5e471e1e0002"
    experiment = uuid4()
    url = f"/experiments/{experiment}"
    app = create_app(seed(tmp_path), runner_token=TOKEN)
    with TestClient(app, raise_server_exceptions=False) as client:
        capfire.exporter.clear()
        runner = {"X-Bazaar-Runner-Token": TOKEN}

        def grant(approval, experiment_id=experiment):
            body = {"approval_id": approval, "experiment_id": str(experiment_id)}
            return client.post("/control/grants", json=body, headers=runner).status_code

        assert grant(malformed) == 422
        wrong = {"X-Bazaar-Runner-Token": "wrong-" + TOKEN}
        body = {"approval_id": str(APPROVAL), "experiment_id": str(experiment)}
        assert client.post("/control/grants", json=body, headers=wrong).status_code == 401
        assert grant(str(APPROVAL)) == 204
        assert grant(str(APPROVAL), uuid4()) == 409

        cutoff = {"cutoff": close_at(SESSIONS[0]).isoformat(), "data_version": "test-v1",
                  "execution_rule_version": "exec-v1"}  # fmt: skip
        for bad in (malformed, unknown):
            sent = {"X-Bazaar-Approval": bad, **runner}
            assert client.put(f"{url}/cutoff", json=cutoff, headers=sent).status_code == 403
        assert client.put(f"{url}/cutoff", json=cutoff, headers=headers()).status_code == 200
        account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
                   "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip
        created = client.post(f"{url}/accounts", json=account, headers=headers()).json()
        news = client.get(
            f"{url}/news/AAPL",
            params={
                "start_at": close_at(SESSIONS[0]).isoformat(),
                "end_at": close_at(SESSIONS[0]).isoformat(),
            },
            headers={"X-Bazaar-Approval": str(APPROVAL), "X-Bazaar-Account": malformed},
        )
        assert news.status_code == 403

        def explode(*args, **kwargs):
            raise RuntimeError("forced failure inside an authorized order")

        monkeypatch.setattr(app_module.Ledger, "_execute", explode)
        order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy",
                 "quantity": "1"}  # fmt: skip
        failed = client.post(
            f"{url}/accounts/{created['account_id']}/orders", json=order, headers=headers()
        )
        assert failed.status_code == 500

    everything = spans(capfire)
    assert any(e["name"] == "exception" for s in everything for e in s.get("events", ())), (
        "the forced failure must be recorded, or this test proves nothing"
    )
    for span in everything:
        text = repr(span)
        for secret in (TOKEN, str(APPROVAL), malformed, unknown):
            assert secret not in text, (secret, span["name"], text[:400])
