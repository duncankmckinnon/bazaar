import hashlib
import itertools
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from bazaar_agent.api import create_app
from bazaar_agent.registry_store import RegistryStore, digest
from bazaar_protocol.registry import (
    CreateStrategyRequest,
    CreateVersionRequest,
    LegacyStrategyDefinition,
    StrategyDefinition,
    StrategyRegistration,
    StrategyVersion,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "registry.sqlite3")) as result:
        yield result


def payload(name="Alpha Trader", **updates):
    values = {
        "name": name,
        "definition": {"instructions": "Maximize portfolio value."},
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
        {"definition": {"instructions": " "}},
        {"definition": {"instructions": ""}},
        {"definition": {"instructions": "x" * 20001}},
        {"definition": {}},
        {"definition": {"instructions": "Trade", "artifact_ref": "reviewed:v1"}},
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


def test_registration_is_independent_of_runtime_model_catalog(tmp_path):
    with TestClient(
        create_app(database_path=tmp_path / "registry.db", model_refs=frozenset({"custom"}))
    ) as client:
        assert register(client).status_code == 201
        body = payload("custom-model", definition={"model_ref": "custom", "instructions": "Trade"})
        assert register(client, body).status_code == 422


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
        "definition": {"instructions": "New strategy"},
        "hypothesis": "Better decisions",
    }
    key = uuid4()
    response = revise(client, strategy_id, body, key)
    assert response.status_code == 201
    second = response.json()
    assert second["version"] == 2
    assert second["parent_version_id"] == first["version_id"]
    assert second["definition_digest"] != first["definition_digest"]
    assert second["definition"] == {"instructions": "New strategy"}
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("harness", "single_shot"),
        ("model_ref", "test"),
        ("tools", ["account", "market_history", "orders"]),
        ("artifact_ref", None),
        ("artifact_ref", "reviewed:v1"),
    ],
)
def test_runtime_fields_rejected_for_create_and_revision(client, field, value):
    initial = register(client).json()
    strategy_id = initial["strategy"]["strategy_id"]
    definition = payload()["definition"] | {field: value}
    assert register(client, payload("runtime", definition=definition)).status_code == 422
    response = revise(client, strategy_id, {"definition": definition})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert client.get("/agents").json()["total"] == 1
    assert client.get(f"/strategies/{strategy_id}").json()["version_count"] == 1
    assert client.get(f"/strategies/{strategy_id}/versions").json()["items"] == [initial["version"]]


def test_legacy_body_cannot_replay_or_create_instructions_only_requests(tmp_path):
    database = tmp_path / "registry.db"
    with TestClient(create_app(database_path=database)) as client:
        create_key, revision_key = uuid4(), uuid4()
        initial = register(client, key=create_key).json()
        strategy_id = initial["strategy"]["strategy_id"]
        assert revise(client, strategy_id, key=revision_key).status_code == 201
        with sqlite3.connect(database) as connection:
            before = list(connection.iterdump())
        legacy_definition = payload()["definition"] | {"model_ref": "test"}
        bodies = (
            ("/strategies", payload(definition=legacy_definition), create_key),
            (
                f"/strategies/{strategy_id}/versions",
                {"definition": legacy_definition},
                revision_key,
            ),
        )
        unused_key = uuid4()
        for route, body, used_key in bodies:
            mismatch = client.post(route, json=body, headers={"Idempotency-Key": str(used_key)})
            assert mismatch.status_code == 409
            assert mismatch.json()["error"]["code"] == "idempotency_conflict"
            assert (
                client.post(
                    route, json=body, headers={"Idempotency-Key": str(unused_key)}
                ).status_code
                == 422
            )
        with sqlite3.connect(database) as connection:
            assert list(connection.iterdump()) == before
        # Failed compatibility attempts must not consume even previously unused keys.
        assert register(client, payload("new-agent"), unused_key).status_code == 201
        assert revise(client, strategy_id, key=unused_key).status_code == 201


def test_definition_contract_is_frozen_and_instructions_only(client):
    definition = StrategyDefinition(instructions="Trade")
    assert definition.model_dump() == {"instructions": "Trade"}
    with pytest.raises(ValidationError, match="frozen_instance"):
        definition.instructions = "Changed"
    legacy = LegacyStrategyDefinition(model_ref="old-model", instructions="Trade")
    for request_type in (CreateStrategyRequest, CreateVersionRequest):
        values = {"name": "legacy"} if request_type is CreateStrategyRequest else {}
        with pytest.raises(ValidationError):
            request_type(**values, definition=legacy)
        with pytest.raises(ValidationError):
            request_type(**values, definition=legacy.model_dump(mode="json"))
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    schema = schemas["StrategyDefinition"]
    assert set(schema["properties"]) == {"instructions"}
    assert schema["additionalProperties"] is False
    assert schemas["CreateStrategyRequest"]["properties"]["definition"] == {
        "$ref": "#/components/schemas/StrategyDefinition"
    }
    assert schemas["CreateVersionRequest"]["properties"]["definition"] == {
        "$ref": "#/components/schemas/StrategyDefinition"
    }


@pytest.mark.parametrize("custom_runtime", [False, True])
def test_legacy_snapshots_digests_and_replay_survive_restart(tmp_path, custom_runtime):
    database = tmp_path / "registry.db"
    create_key, revision_key = uuid4(), uuid4()
    with TestClient(create_app(database_path=database)) as client:
        initial = register(client, key=create_key).json()
        strategy_id = initial["strategy"]["strategy_id"]
        second = revise(client, strategy_id, key=revision_key).json()

    # Seed the exact pre-PR39 wire shape, including its normalized defaults.
    # Do not derive the expected snapshots or hashes from the compatibility model.
    legacy = {
        "harness": "research" if custom_runtime else "single_shot",
        "model_ref": "retired-model" if custom_runtime else "test",
        "instructions": "Maximize portfolio value.",
        "tools": ["news", "reports"] if custom_runtime else ["account", "market_history", "orders"],
        "artifact_ref": "reviewed:旧-strategy" if custom_runtime else None,
    }
    encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    old_digest = hashlib.sha256(encoded.encode()).hexdigest()
    for version in (initial["version"], second):
        version["definition"] = legacy
        version["definition_digest"] = old_digest
    historical_create = payload() | {
        "name": "alpha-trader",
        "description": "",
        "definition": legacy,
        "parent_version_id": None,
    }
    historical_revision = {"definition": legacy, "hypothesis": "", "parent_version_id": None}
    replay_entries = (
        ("create", create_key, historical_create, initial, StrategyRegistration),
        (strategy_id, revision_key, historical_revision, second, StrategyVersion),
    )
    with sqlite3.connect(database) as connection:
        for version in (initial["version"], second):
            connection.execute(
                "UPDATE registry_versions SET definition = ?, definition_digest = ? WHERE version_id = ?",
                (json.dumps(legacy), old_digest, version["version_id"]),
            )
        for scope, key, request, response, _ in replay_entries:
            connection.execute(
                "UPDATE registry_requests SET fingerprint = ?, response = ? WHERE scope = ? AND request_key = ?",
                (digest(request), json.dumps(response), scope, str(key)),
            )
        before = list(connection.iterdump())

    with TestClient(create_app(database_path=database, model_refs=frozenset())) as client:
        store = client.app.state.registry
        for version in (initial["version"], second):
            path = f"/strategies/{strategy_id}/versions/{version['version_id']}"
            assert client.get(path).json() == version
        assert client.get(f"/strategies/{strategy_id}/versions").json()["items"] == [
            initial["version"],
            second,
        ]
        for scope, key, request, response, response_type in replay_entries:
            legacy_strategy_id = None if scope == "create" else UUID(scope)
            restored = store.replay_legacy_request(request, key, strategy_id=legacy_strategy_id)
            assert isinstance(restored, response_type)
            assert restored.model_dump(mode="json") == response
            version = restored.version if isinstance(restored, StrategyRegistration) else restored
            assert isinstance(version.definition, LegacyStrategyDefinition)
            assert version.definition_digest == old_digest

            route = "/strategies" if scope == "create" else f"/strategies/{scope}/versions"
            headers = {"Idempotency-Key": str(key)}
            exact = client.post(route, json=request, headers=headers)
            assert exact.status_code == 201
            assert exact.json() == response

            # Historical request defaults, name normalization and tool sorting must
            # produce the old fingerprint, not one based on stripped runtime fields.
            normalized_retry = {"definition": legacy | {"tools": list(reversed(legacy["tools"]))}}
            if scope == "create":
                normalized_retry["name"] = "  ALPHA___Trader  "
            if not custom_runtime:
                normalized_retry["definition"] = {
                    "model_ref": "test",
                    "instructions": legacy["instructions"],
                }
            retry = client.post(route, json=normalized_retry, headers=headers)
            assert retry.status_code == 201
            assert retry.json() == response

            unused = client.post(route, json=request, headers={"Idempotency-Key": str(uuid4())})
            assert unused.status_code == 422
            changed = request | {"definition": legacy | {"model_ref": "different-model"}}
            conflict = client.post(route, json=changed, headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "idempotency_conflict"
            for invalid in (
                request | {"credentials": "secret-value"},
                request | {"definition": legacy | {"tools": ["orders", "orders"]}},
                request | {"definition": legacy | {"harness": "unknown"}},
                request | {"definition": legacy | {"instructions": " "}},
                request | {"parent_version_id": "bad-uuid"},
                request | {"definition": legacy | {"runtime_settings": {}}},
            ):
                rejected = client.post(route, json=invalid, headers=headers)
                assert rejected.status_code == 422
                assert "secret-value" not in rejected.text
            for invalid_headers in ({}, {"Idempotency-Key": "bad-uuid"}):
                assert client.post(route, json=request, headers=invalid_headers).status_code == 422
            assert client.post(route, content="{", headers=headers).status_code == 422

        # The creation key cannot replay the revision scope or another strategy.
        assert revise(client, strategy_id, historical_revision, create_key).status_code == 422
        assert revise(client, uuid4(), historical_revision, revision_key).status_code == 422
        assert revise(client, "bad-uuid", historical_revision, revision_key).status_code == 422
        assert (
            client.put(
                "/strategies", json=historical_create, headers={"Idempotency-Key": str(create_key)}
            ).status_code
            == 405
        )
        assert register(client, key=create_key).status_code == 409
        assert revise(client, strategy_id, key=revision_key).status_code == 409
        with sqlite3.connect(database) as connection:
            assert list(connection.iterdump()) == before

        new_version = revise(client, strategy_id).json()
        assert new_version["version"] == 3
        assert new_version["parent_version_id"] == second["version_id"]
        assert new_version["definition"] == payload()["definition"]
        assert new_version["definition_digest"] != old_digest
        versions = client.get(f"/strategies/{strategy_id}/versions").json()["items"]
        assert versions == [initial["version"], second, new_version]
        with sqlite3.connect(database) as connection:
            after_revision = list(connection.iterdump())
        assert register(client, historical_create, create_key).json() == initial
        assert revise(client, strategy_id, historical_revision, revision_key).json() == second
        with sqlite3.connect(database) as connection:
            assert list(connection.iterdump()) == after_revision
