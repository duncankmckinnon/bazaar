"""The Docker smoke fixture imports as demo-bundle-v2, offline, the same way data-init does."""

import importlib.util
from pathlib import Path

from bazaar_market.app import create_app
from bazaar_market.sources.cli import main
from fastapi.testclient import TestClient

REPO = Path(__file__).parents[2]
CONFIG = str(REPO / "config" / "docker-smoke-sources.toml")
spec = importlib.util.spec_from_file_location(
    "docker_snapshots", Path(__file__).with_name("docker_snapshots.py")
)
docker_snapshots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker_snapshots)
WINDOW = ["--required-from", "2026-09-21", "--required-to", "2026-10-09"]


def data_init(root: Path, universe_csv: Path, db: Path) -> list[int]:
    """docker-compose.yml data-init's import sequence, with the CI env values."""
    common = ["--config", CONFIG, "--universe-csv", str(universe_csv), "--db", str(db)]
    version = docker_snapshots.VERSION
    return [
        main(["import-bars", *common, "--snapshot", str(root / "alpaca-bars" / version),
              "--data-version", "alpaca-bars-v2", *WINDOW, "--required-symbols", "SPY"]),
        main(["import-universe", *common, "--bundle", "demo-bundle-v2",
              "--data-version", "alpaca-bars-v2", *WINDOW]),
        main(["import-news", *common, "--snapshot", str(root / "alpaca-news" / version),
              "--data-version", "alpaca-news-v2"]),
        main(["import-filings", *common, "--snapshot", str(root / "edgar" / version),
              "--data-version", "edgar-filings-v2"]),
    ]  # fmt: skip


def test_the_smoke_fixture_serves_demo_bundle_v2_with_a_universe(tmp_path):
    universe_csv = docker_snapshots.write(tmp_path / "snapshots")
    db = tmp_path / "market.sqlite3"

    assert data_init(tmp_path / "snapshots", universe_csv, db) == [0, 0, 0, 0]

    with TestClient(create_app(database_path=db, runner_token="ci-only-runner-token")) as client:
        bundle = client.get("/bundles/demo-bundle-v2").json()
    assert [m["symbol"] for m in bundle["universe"]] == ["AAAA", "AAPL", "BBBB"]
    assert bundle["benchmark"] == "SPY"
    assert len(bundle["sessions"]) == bundle["expected_sessions"] == 10
    assert bundle["incomplete_sessions"] == []


def test_the_fixture_is_synthetic():
    # Never real constituents: invented names and CIKs only.
    assert all(cik >= 9_000_000 for *_, cik in docker_snapshots.UNIVERSE)
    assert {s for s, *_ in docker_snapshots.UNIVERSE} == {"AAPL", "AAAA", "BBBB"}
