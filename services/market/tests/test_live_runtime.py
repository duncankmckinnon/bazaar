"""deploy/live_runtime.py, offline: seed data, the market subprocess, and volume commits."""

import importlib.util
import socket
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[3] / "deploy" / "live_runtime.py"
spec = importlib.util.spec_from_file_location("live_runtime", PATH)
rt = importlib.util.module_from_spec(spec)
sys.modules["live_runtime"] = rt
spec.loader.exec_module(rt)

TOKEN = "dummy-runner-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def seed(tmp_path):
    folder = tmp_path / "seed"
    (folder / "runs" / "baseline").mkdir(parents=True)
    (folder / "runs" / "baseline" / "run.json").write_text("{}")
    with closing(sqlite3.connect(folder / "market.sqlite3")) as connection:
        connection.execute("CREATE TABLE seeded (x)")
        connection.commit()
    return folder


def tables(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as connection:
        return {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}


def test_prepare_data_copies_the_seed_onto_an_empty_volume(tmp_path, seed):
    db, runs = rt.prepare_data(tmp_path / "data", seed / "market.sqlite3", seed / "runs")

    assert "seeded" in tables(db)
    assert (runs / "baseline" / "run.json").exists()
    assert not list((tmp_path / "data").glob("*.part"))


def test_prepare_data_never_overwrites_what_the_volume_already_has(tmp_path, seed):
    volume = tmp_path / "data"
    (volume / "runs").mkdir(parents=True)
    with closing(sqlite3.connect(volume / "market.sqlite3")) as connection:
        connection.execute("CREATE TABLE live (x)")
        connection.commit()

    db, runs = rt.prepare_data(volume, seed / "market.sqlite3", seed / "runs")

    assert tables(db) == {"live"}
    assert list(runs.iterdir()) == []


def test_prepare_data_without_a_seed_or_database_fails(tmp_path):
    with pytest.raises(FileNotFoundError, match="no seed"):
        rt.prepare_data(tmp_path / "data", tmp_path / "missing.sqlite3", None)


def test_prepare_data_without_seed_runs_leaves_an_empty_runs_folder(tmp_path, seed):
    _, runs = rt.prepare_data(tmp_path / "data", seed / "market.sqlite3", None)

    assert runs.is_dir() and list(runs.iterdir()) == []


def test_a_missing_runner_token_fails_fast_without_starting_anything(tmp_path, seed):
    with pytest.raises(rt.MissingRunnerToken, match="BAZAAR_RUNNER_TOKEN is not set"):
        rt.start_market(seed / "market.sqlite3", port=free_port(), env={"PATH": "/usr/bin"})


def test_the_market_starts_on_the_volume_database_and_answers_health(tmp_path, seed):
    db, _ = rt.prepare_data(tmp_path / "data", seed / "market.sqlite3", seed / "runs")
    port = free_port()
    env = {"PATH": "/usr/bin", "BAZAAR_RUNNER_TOKEN": TOKEN}

    market = rt.start_market(db, port=port, env=env)
    try:
        rt.wait_healthy(f"http://127.0.0.1:{port}/health", timeout=30, process=market)
        assert {"seeded", "acct_experiments"} <= tables(db)  # the market opened the volume copy
    finally:
        market.terminate()
        market.wait(timeout=10)


def test_wait_healthy_raises_when_the_market_exits(tmp_path):
    exited = rt.subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])

    with pytest.raises(rt.MarketNotReady, match="status 3"):
        rt.wait_healthy(f"http://127.0.0.1:{free_port()}/health", timeout=10, process=exited)


def test_wait_healthy_raises_when_nothing_answers_in_time():
    with pytest.raises(rt.MarketNotReady, match="did not answer"):
        rt.wait_healthy(f"http://127.0.0.1:{free_port()}/health", timeout=0.5)


class Commits:
    def __init__(self, fail: bool = False) -> None:
        self.count = 0
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self) -> None:
        with self.lock:
            self.count += 1
        if self.fail:
            raise RuntimeError("volume unavailable")


def test_the_volume_is_committed_on_a_timer():
    commits = Commits()
    committer = rt.VolumeCommitter(commits, interval=0.05).start()
    time.sleep(0.3)
    committer.stop()

    assert commits.count >= 3


def test_on_scored_commits_the_volume_at_once():
    commits = Commits()
    committer = rt.VolumeCommitter(commits, interval=3600)

    committer.on_scored("sub-1", Path("/data/runs/sub-1"))

    assert commits.count == 1


def test_a_failed_commit_is_logged_and_never_raised(caplog):
    committer = rt.VolumeCommitter(Commits(fail=True), interval=3600)

    with caplog.at_level("ERROR", logger="bazaar.live"):
        committer.on_scored("sub-1", Path("/data/runs/sub-1"))  # must not raise

    assert "volume commit failed (scored sub-1)" in caplog.text


def test_stop_makes_a_last_commit():
    commits = Commits()
    committer = rt.VolumeCommitter(commits, interval=3600).start()

    committer.stop()

    assert commits.count == 1
