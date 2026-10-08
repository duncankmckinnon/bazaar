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


def test_the_log_says_when_the_database_was_freshly_seeded(tmp_path, seed, caplog):
    with caplog.at_level("INFO", logger="bazaar.live"):
        rt.prepare_data(tmp_path / "data", seed / "market.sqlite3", seed / "runs")

    assert "freshly seeded from" in caplog.text


def test_the_log_says_when_the_volume_database_was_kept(tmp_path, seed, caplog):
    rt.prepare_data(tmp_path / "data", seed / "market.sqlite3", seed / "runs")
    caplog.clear()

    with caplog.at_level("INFO", logger="bazaar.live"):
        rt.prepare_data(tmp_path / "data", seed / "market.sqlite3", seed / "runs")

    assert "already on the volume; seed not applied" in caplog.text


def short_lived(code: int = 3) -> "rt.subprocess.Popen":
    return rt.subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep(0.2); raise SystemExit({code})"]
    )


def test_the_watchdog_exits_the_container_when_the_market_dies(caplog):
    exits: list[int] = []

    with caplog.at_level("ERROR", logger="bazaar.live"):
        rt.watch_market(short_lived(), threading.Event(), exit=exits.append).join(timeout=10)

    assert exits == [1]
    assert "the market exited with status 3" in caplog.text


def test_the_watchdog_ignores_a_market_stopped_by_shutdown():
    exits: list[int] = []
    stopping = threading.Event()
    stopping.set()

    rt.watch_market(short_lived(0), stopping, exit=exits.append).join(timeout=10)

    assert exits == []


def sleeper(ignore_term: bool = False) -> "rt.subprocess.Popen":
    code = "import signal, time\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += "print('ready', flush=True)\ntime.sleep(60)\n"
    process = rt.subprocess.Popen([sys.executable, "-c", code], stdout=rt.subprocess.PIPE)
    process.stdout.readline()  # the signal handler is in place
    return process


@pytest.mark.parametrize("ignore_term", [False, True])
def test_shutdown_commits_only_after_the_market_has_exited(ignore_term):
    market = sleeper(ignore_term)
    exited_at_commit: list[bool] = []
    committer = rt.VolumeCommitter(lambda: exited_at_commit.append(market.poll() is not None))
    stopping = threading.Event()

    rt.shutdown(market, committer, stopping, timeout=1)

    assert exited_at_commit == [True]
    assert stopping.is_set()


def test_the_watchdog_commits_the_volume_before_it_exits():
    events: list[str] = []
    committer = rt.VolumeCommitter(lambda: events.append("commit"), interval=3600)

    rt.watch_market(
        short_lived(),
        threading.Event(),
        exit=lambda code: events.append(f"exit {code}"),
        before_exit=lambda: committer.commit_now("market exited"),
    ).join(timeout=10)

    assert events == ["commit", "exit 1"]


def test_the_watchdog_still_exits_when_the_last_commit_fails(caplog):
    exits: list[int] = []

    def broken() -> None:
        raise RuntimeError("volume unavailable")

    with caplog.at_level("ERROR", logger="bazaar.live"):
        rt.watch_market(
            short_lived(), threading.Event(), exit=exits.append, before_exit=broken
        ).join(timeout=10)

    assert exits == [1]
    assert "the last commit before exiting failed" in caplog.text


T1 = rt.datetime(2026, 10, 8, 18, 0, tzinfo=rt.UTC)
T2 = rt.datetime(2026, 10, 8, 19, 0, tzinfo=rt.UTC)


def board(volume: Path, *names: str) -> None:
    for name in names:
        (volume / "runs" / name).mkdir(parents=True)
        (volume / "runs" / name / "run.json").write_text(name)


def new_seed(tmp_path, *names: str) -> Path:
    folder = tmp_path / "seed-v2"
    for name in names:
        (folder / name).mkdir(parents=True)
        (folder / name / "run.json").write_text(name)
    return folder


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_a_reseed_archives_the_board_and_copies_the_new_seed(tmp_path, caplog):
    volume = tmp_path / "data"
    board(volume, "baseline", "attendee-1")
    commits = Commits()

    with caplog.at_level("INFO", logger="bazaar.live"):
        outcome = rt.reseed_runs(volume, new_seed(tmp_path, "v2-a", "v2-b"), "v2", T1, commits)

    assert outcome == "reseeded"
    assert listing(volume / "runs") == ["v2-a", "v2-b"]
    assert listing(volume / "archive" / "20261008T180000Z" / "runs") == ["attendee-1", "baseline"]
    assert (volume / "archive" / "reseeded-v2").exists()
    assert commits.count == 1
    assert "copied 2 seed runs" in caplog.text and "archived the previous runs" in caplog.text


def test_the_same_id_reseeds_once_across_restarts(tmp_path, caplog):
    volume = tmp_path / "data"
    board(volume, "baseline")
    seed = new_seed(tmp_path, "v2-a")
    rt.reseed_runs(volume, seed, "v2", T1)
    board(volume, "attendee-scored-after")  # a run scored after the reseed

    with caplog.at_level("INFO", logger="bazaar.live"):
        outcome = rt.reseed_runs(volume, seed, "v2", T2)  # the container restarted

    assert outcome == "skipped"
    assert listing(volume / "runs") == ["attendee-scored-after", "v2-a"]
    assert listing(volume / "archive") == ["20261008T180000Z", "reseeded-v2"]
    assert "skipped" in caplog.text


def test_a_new_id_reseeds_again(tmp_path):
    volume = tmp_path / "data"
    board(volume, "baseline")
    seed = new_seed(tmp_path, "v2-a")
    rt.reseed_runs(volume, seed, "v2", T1)

    assert rt.reseed_runs(volume, seed, "v3", T2) == "reseeded"
    assert listing(volume / "archive" / "20261008T190000Z" / "runs") == ["v2-a"]


@pytest.mark.parametrize("flag", [None, "", "   "])
def test_without_the_flag_nothing_happens(tmp_path, flag):
    volume = tmp_path / "data"
    board(volume, "baseline")

    assert rt.reseed_runs(volume, new_seed(tmp_path, "v2-a"), flag, T1) == "off"
    assert listing(volume / "runs") == ["baseline"]
    assert not (volume / "archive").exists()


@pytest.mark.parametrize("seed_names", [None, ()])
def test_missing_or_empty_seeds_fail_closed_and_leave_the_board(tmp_path, seed_names):
    volume = tmp_path / "data"
    board(volume, "baseline", "attendee-1")
    seed = tmp_path / "seed-v2"
    if seed_names is not None:
        seed.mkdir()

    with pytest.raises(FileNotFoundError, match="nothing moved"):
        rt.reseed_runs(volume, seed, "v2", T1)

    assert listing(volume / "runs") == ["attendee-1", "baseline"]
    assert not (volume / "archive").exists()


def test_the_marker_is_written_only_after_a_successful_copy(tmp_path, monkeypatch):
    volume = tmp_path / "data"
    board(volume, "baseline")

    def broken_copy(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(rt.shutil, "copytree", broken_copy)
    with pytest.raises(OSError, match="disk full"):
        rt.reseed_runs(volume, new_seed(tmp_path, "v2-a"), "v2", T1)

    assert not (volume / "archive" / "reseeded-v2").exists()
    assert listing(volume / "runs") == ["baseline"]


def test_an_unsafe_reseed_id_is_refused(tmp_path):
    with pytest.raises(ValueError, match="BAZAAR_RESEED_RUNS"):
        rt.reseed_runs(tmp_path / "data", new_seed(tmp_path, "v2-a"), "../escape", T1)


def test_a_failed_commit_after_a_reseed_is_logged_not_fatal(tmp_path, caplog):
    volume = tmp_path / "data"
    board(volume, "baseline")

    with caplog.at_level("ERROR", logger="bazaar.live"):
        outcome = rt.reseed_runs(volume, new_seed(tmp_path, "v2-a"), "v2", T1, Commits(fail=True))

    assert outcome == "reseeded"
    assert "volume commit failed" in caplog.text
