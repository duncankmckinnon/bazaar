import json
from uuid import uuid4

from bazaar_agent.api import create_app
from fastapi.testclient import TestClient


def test_registry_traces_operations_database_and_http_without_payloads(tmp_path, capfire):
    with TestClient(create_app(database_path=tmp_path / "registry.db")) as client:
        key = str(uuid4())
        payload = {
            "name": "Monitored Agent",
            "definition": {
                "instructions": "PRIVATE-INSTRUCTIONS-12345",
            },
        }
        headers = {"Idempotency-Key": key, "Authorization": "Bearer PRIVATE-TOKEN-12345"}
        response = client.post("/strategies", json=payload, headers=headers)
        assert response.status_code == 201
        result = response.json()
        assert client.post("/strategies", json=payload, headers=headers).json() == result
        version = client.post(
            f"/strategies/{result['strategy']['strategy_id']}/versions",
            json={"definition": payload["definition"], "hypothesis": "PRIVATE-HYPOTHESIS-12345"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert version.status_code == 201
        # Rejected runtime fields must remain payload-safe too.
        rejected = client.post(
            "/strategies",
            json={
                **payload,
                "definition": {
                    **payload["definition"],
                    "artifact_ref": "https://example.invalid/PRIVATE-ARTIFACT-12345",
                },
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert rejected.status_code == 422
        assert client.get("/agents").status_code == 200
    spans = capfire.exporter.exported_spans_as_dict()
    names = {span["name"] for span in spans}
    assert {"registry.create", "registry.add_version", "registry.list_agents"} <= names
    assert "Registered named strategy" in names
    assert "Replayed strategy registration" in names
    assert any("registry_" in span["attributes"].get("db.statement", "") for span in spans)
    assert any(
        span["attributes"].get(
            "http.status_code", span["attributes"].get("http.response.status_code")
        )
        == 201
        for span in spans
    )
    serialized = json.dumps(spans, default=str)
    for marker in (
        "PRIVATE-INSTRUCTIONS",
        "PRIVATE-ARTIFACT",
        "PRIVATE-HYPOTHESIS",
        "PRIVATE-TOKEN",
    ):
        assert marker not in serialized
    metadata = next(
        span["attributes"] for span in spans if span["name"] == "Registered named strategy"
    )
    assert metadata["agent_id"] == result["agent"]["agent_id"]
    assert metadata["strategy_id"] == result["strategy"]["strategy_id"]


def test_validation_and_conflict_events_are_monitored(tmp_path, capfire):
    with TestClient(create_app(database_path=tmp_path / "registry.db")) as client:
        invalid = client.post("/strategies", json={"credentials": "PRIVATE-BAD-PAYLOAD"})
        assert invalid.status_code == 422
        body = {"name": "one", "definition": {"instructions": "Trade"}}
        assert (
            client.post(
                "/strategies", json=body, headers={"Idempotency-Key": str(uuid4())}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/strategies", json=body, headers={"Idempotency-Key": str(uuid4())}
            ).status_code
            == 409
        )
        assert client.get("/health").status_code == 200
    spans = capfire.exporter.exported_spans_as_dict()
    assert any(span["name"] == "Invalid registry request" for span in spans)
    assert any(
        span["name"] == "Registry request rejected" and span["attributes"]["status_code"] == 409
        for span in spans
    )
    assert "PRIVATE-BAD-PAYLOAD" not in json.dumps(spans, default=str)
    assert not any("/health" in span["name"] for span in spans)
