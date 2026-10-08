"""Deployment contracts, rendered by Compose without contacting a Docker daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def stack():
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is not installed")
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "config", "--format", "json"],
        cwd=root,
        env={"PATH": os.environ["PATH"], "BAZAAR_RUNNER_TOKEN": "compose-test-token"},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def volume_for(service, target):
    return next(v for v in service["volumes"] if v["target"] == target)


def test_default_stack_starts_web_worker_with_market_credentials(stack):
    services = stack["services"]
    assert "web" in services, "Compose must start the submission worker, not just placeholders"
    web, market = services["web"], services["market"]
    assert not web.get("profiles")
    assert web["depends_on"]["market"]["condition"] == "service_healthy"
    assert web["environment"]["BAZAAR_MARKET_URL"] == "http://market:8000"
    assert web["environment"]["BAZAAR_RUNNER_TOKEN"] == market["environment"]["BAZAAR_RUNNER_TOKEN"]
    assert web["environment"]["BAZAAR_RUNNER_TOKEN"]
    for name in ("PYDANTIC_AI_GATEWAY_API_KEY", "LOGFIRE_TOKEN", "BAZAAR_STRATEGY_EVAL_ENABLED"):
        assert name in web["environment"]


def test_market_cannot_start_before_snapshot_import(stack):
    services = stack["services"]
    assert "data-init" in services, "Empty market data must block startup"
    assert services["market"]["depends_on"]["data-init"]["condition"] == (
        "service_completed_successfully"
    )
    market_volume = volume_for(services["market"], "/data")
    assert market_volume["type"] == "volume"
    assert volume_for(services["data-init"], "/data")["source"] == market_volume["source"]
    assert volume_for(services["data-init"], "/snapshots")["read_only"]


def test_application_state_survives_container_recreation_without_host_database(stack):
    for name, db_variable in (
        ("market", "BAZAAR_MARKET_DB"),
        ("web", "BAZAAR_WEB_DB"),
        ("api", "BAZAAR_REGISTRY_DB_PATH"),
    ):
        service = stack["services"][name]
        assert service["environment"][db_variable].startswith("/data/")
        assert volume_for(service, "/data")["type"] == "volume"
        assert all(v["type"] == "volume" for v in service["volumes"])
    assert stack["services"]["web"]["environment"]["BAZAAR_RUNS_DIR"].startswith("/data/")
