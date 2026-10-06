import hashlib
import itertools
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from bazaar_agent.api import create_app
from bazaar_agent.registry_store import RegistryStore
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "registry.sqlite3")) as result:
        yield result


def payload(name="Alpha Trader", **updates):
    values = {
        "name": name,
        "definition": {"model_ref": "test", "instructions": "Maximize portfolio value."},
    }
    return values | updates


def register(client, body=None, key=None):
    return client.post(
        "/strategies", json=body or payload(), headers={"Idempotency-Key": str(key or uuid4())}
    )


def revise(client, strategy_id, body=None, key=None):
    return client.post(
        f"/strategies/{strategy_id}/versions",
        json=body or {"definition": payload()["definition"]},
        headers={"Idempotency-Key": str(key or uuid4())},
    )


def test_empty_registry_health_and_schema(client):
    assert client.get("/health").json() == {"status": "ok"}
    for route in ("/agents", "/strategies"):
        assert client.get(route).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert "CreateStrategyRequest" in client.get("/openapi.json").json()["components"]["schemas"]


def test_atomic_registration_and_retrieval(client):
    response = register(client)
    assert response.status_code == 201
    result = response.json()
    agent, strategy, version = result["agent"], result["strategy"], result["version"]
    assert result["status"] == "proposed"
    assert agent["name"] == "alpha-trader"
    assert agent["strategy_id"] == strategy["strategy_id"] == version["strategy_id"]
    assert strategy["agent_id"] == agent["agent_id"]
    assert strategy["version_count"] == version["version"] == 1
    assert strategy["latest_version_id"] == version["version_id"]
    assert version["parent_version_id"] is None
    assert agent["created_by"] == strategy["created_by"] == version["created_by"] == "local-api"
    assert client.get(f"/agents/{agent['agent_id']}").json() == agent
    assert client.get(f"/strategies/{strategy['strategy_id']}").json() == strategy
    assert (
        client.get(f"/strategies/{strategy['strategy_id']}/versions/{version['version_id']}").json()
        == version
    )
    encoded = json.dumps(
        version["definition"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert version["definition_digest"] == hashlib.sha256(encoded.encode()).hexdigest()
    assert client.get("/agents").json()["items"] == [agent]
    assert client.get("/strategies").json()["items"] == [strategy]


def test_normalized_names_and_conflict(client):
    assert register(client, payload("  ALPHA___Trader  ")).status_code == 201
    conflict = register(client, payload("alpha-trader"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    assert client.get("/agents").json()["total"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "123-strategy"},
        {"name": "a" * 65},
        {"name": "other/name"},
        {"definition": {"model_ref": "test", "instructions": " "}},
        {"definition": {"model_ref": "unknown", "instructions": "Trade"}},
        {"definition": {"model_ref": "test", "instructions": "Trade", "harness": "invalid"}},
        {"definition": {"model_ref": "test", "instructions": "Trade", "tools": ["secret_tool"]}},
        {
            "definition": {
                "model_ref": "test",
                "instructions": "Trade",
                "tools": ["orders", "orders"],
            }
        },
        {"created_by": "forged-actor"},
        {"credentials": "secret-value"},
        {"code": "import os"},
    ],
)
def test_invalid_registration_has_no_side_effects(client, change):
    response = register(
        client, payload(**change) if "name" not in change else payload(change["name"])
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "secret-value" not in response.text
    assert client.get("/agents").json()["total"] == 0
    assert client.get("/strategies").json()["total"] == 0


def test_model_catalog_is_configurable(tmp_path):
    with TestClient(
        create_app(database_path=tmp_path / "registry.db", model_refs=frozenset({"custom"}))
    ) as client:
        assert register(client).status_code == 422
        body = payload(definition={"model_ref": "custom", "instructions": "Trade"})
        assert register(client, body).status_code == 201


def test_required_valid_idempotency_header(client):
    for headers in ({}, {"Idempotency-Key": "invalid"}):
        response = client.post("/strategies", json=payload(), headers=headers)
        assert response.status_code == 422
    assert client.get("/agents").json()["total"] == 0


def test_idempotent_create_and_conflicting_content(client):
    key = uuid4()
    initial = register(client, key=key).json()
    strategy_id = initial["strategy"]["strategy_id"]
    assert revise(client, strategy_id).status_code == 201
    assert register(client, payload("  alpha_trader "), key).json() == initial
    conflict = register(client, payload("another-name"), key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert client.get("/agents").json()["total"] == 1


def test_versions_immutable_and_parent_lineage(client):
    initial = register(client).json()
    strategy_id = initial["strategy"]["strategy_id"]
    first = initial["version"]
    body = {
        "definition": {
            "model_ref": "test",
            "instructions": "New strategy",
            "tools": ["orders", "account"],
        },
        "hypothesis": "Better decisions",
    }
    key = uuid4()
    response = revise(client, strategy_id, body, key)
    assert response.status_code == 201
    second = response.json()
    assert second["version"] == 2
    assert second["parent_version_id"] == first["version_id"]
    assert second["definition_digest"] != first["definition_digest"]
    assert second["definition"]["tools"] == ["account", "orders"]
    body["definition"]["tools"].reverse()
    assert revise(client, strategy_id, body, key).json() == second
    body["hypothesis"] = "Changed request"
    assert revise(client, strategy_id, body, key).status_code == 409
    assert client.get(f"/strategies/{strategy_id}/versions/{first['version_id']}").json() == first
    assert client.get(f"/strategies/{strategy_id}").json()["version_count"] == 2
    assert (
        client.get(f"/strategies/{strategy_id}").json()["latest_version_id"] == second["version_id"]
    )
    versions = client.get(f"/strategies/{strategy_id}/versions?limit=1&offset=1").json()
    assert versions["items"] == [second] and versions["total"] == 2


def test_cross_strategy_parent_and_scoped_idempotency(client):
    key = uuid4()
    original = register(client, key=key).json()
    child = register(
        client, payload("child", parent_version_id=original["version"]["version_id"])
    ).json()
    assert child["agent"]["agent_id"] != original["agent"]["agent_id"]
    assert child["version"]["parent_version_id"] == original["version"]["version_id"]
    for result in (original, child):
        assert revise(client, result["strategy"]["strategy_id"], key=key).status_code == 201
    wrong_strategy = original["strategy"]["strategy_id"]
    wrong_version = child["version"]["version_id"]
    assert client.get(f"/strategies/{wrong_strategy}/versions/{wrong_version}").status_code == 404


def test_missing_resources_and_parent_roll_back(client):
    missing = str(uuid4())
    for route in (
        f"/agents/{missing}",
        f"/strategies/{missing}",
        f"/strategies/{missing}/versions",
    ):
        response = client.get(route)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert register(client, payload(parent_version_id=missing)).status_code == 404
    assert revise(client, missing).status_code == 404
    assert client.get("/agents").json()["total"] == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=invalid"])
def test_invalid_pagination(client, query):
    assert client.get(f"/agents?{query}").status_code == 422
    assert client.get(f"/strategies?{query}").status_code == 422


def test_bounded_pagination(client):
    register(client, payload("one"))
    register(client, payload("two"))
    for route in ("/agents", "/strategies"):
        first = client.get(f"{route}?limit=1").json()
        second = client.get(f"{route}?limit=1&offset=1").json()
        assert first["total"] == second["total"] == 2
        assert len(first["items"]) == len(second["items"]) == 1
        assert first["items"] != second["items"]
        assert client.get(f"{route}?offset=2").json()["items"] == []


def test_persistence_across_restart_and_no_execution_tables(tmp_path):
    database = tmp_path / "registry.db"
    with TestClient(create_app(database_path=database)) as client:
        key = uuid4()
        original = register(client, key=key).json()
    with TestClient(create_app(database_path=database)) as client:
        assert register(client, key=key).json() == original
        assert client.get(f"/agents/{original['agent']['agent_id']}").json() == original["agent"]
        assert client.get("/accounts").status_code == 404
        assert client.post("/experiments", json={}).status_code == 404
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {
        "registry_agents",
        "registry_strategies",
        "registry_versions",
        "registry_requests",
    }


def test_failed_creation_rolls_back_everything(tmp_path, monkeypatch):
    original = RegistryStore._insert_version

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    with TestClient(
        create_app(database_path=tmp_path / "registry.db"), raise_server_exceptions=False
    ) as client:
        key = uuid4()
        monkeypatch.setattr(RegistryStore, "_insert_version", fail)
        response = register(client, key=key)
        assert response.status_code == 500
        assert "simulated write failure" not in response.text
        assert client.get("/agents").json()["total"] == 0
        assert client.get("/strategies").json()["total"] == 0
        monkeypatch.setattr(RegistryStore, "_insert_version", original)
        assert register(client, key=key).status_code == 201


def test_concurrent_requests_idempotency_and_version_numbers(client):
    key = uuid4()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: register(client, key=key), range(4)))
    assert all(response.status_code == 201 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    strategy_id = responses[0].json()["strategy"]["strategy_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        revisions = list(pool.map(lambda _: revise(client, strategy_id), range(4)))
    assert all(response.status_code == 201 for response in revisions)
    assert {response.json()["version"] for response in revisions} == {2, 3, 4, 5}
    versions = client.get(f"/strategies/{strategy_id}/versions").json()["items"]
    assert [version["version"] for version in versions] == [1, 2, 3, 4, 5]
    for previous, current in itertools.pairwise(versions):
        assert current["parent_version_id"] == previous["version_id"]
