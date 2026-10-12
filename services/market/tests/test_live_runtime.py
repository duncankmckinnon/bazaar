"""deploy/live_runtime.py, offline: seed data, the market subprocess, and volume commits."""

import dataclasses
import importlib.util
import socket
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
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
        (folder / name / "record.json").write_text(name)
        (folder / name / "evaluation.json").write_text(name)
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

    def broken_copy(source, destination, **kwargs):
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


def test_a_seed_of_only_dotfiles_fails_closed(tmp_path):
    volume = tmp_path / "data"
    board(volume, "baseline")
    seed = tmp_path / "seed-v2"
    seed.mkdir()
    (seed / ".DS_Store").write_text("finder")

    with pytest.raises(FileNotFoundError, match="no seed runs.*nothing moved"):
        rt.reseed_runs(volume, seed, "v2", T1)

    assert listing(volume / "runs") == ["baseline"]
    assert not (volume / "archive").exists()


@pytest.mark.parametrize("missing", ["record.json", "evaluation.json"])
def test_a_seed_run_missing_a_file_fails_closed_and_names_it(tmp_path, missing):
    volume = tmp_path / "data"
    board(volume, "baseline")
    seed = new_seed(tmp_path, "v2-a", "v2-broken")
    (seed / "v2-broken" / missing).unlink()

    with pytest.raises(FileNotFoundError, match="v2-broken in .*nothing moved"):
        rt.reseed_runs(volume, seed, "v2", T1)

    assert listing(volume / "runs") == ["baseline"]
    assert not (volume / "archive").exists()
    assert not (volume / "runs.part").exists()


def test_a_stray_file_in_the_seed_fails_closed(tmp_path):
    volume = tmp_path / "data"
    board(volume, "baseline")
    seed = new_seed(tmp_path, "v2-a")
    (seed / "notes.txt").write_text("not a run")

    with pytest.raises(FileNotFoundError, match="notes.txt"):
        rt.reseed_runs(volume, seed, "v2", T1)

    assert listing(volume / "runs") == ["baseline"]


def test_a_valid_seed_with_a_stray_dotfile_reseeds_without_it(tmp_path, caplog):
    volume = tmp_path / "data"
    board(volume, "baseline")
    seed = new_seed(tmp_path, "v2-a")
    (seed / ".DS_Store").write_text("finder")
    (seed / "v2-a" / ".DS_Store").write_text("finder")

    with caplog.at_level("INFO", logger="bazaar.live"):
        assert rt.reseed_runs(volume, seed, "v2", T1) == "reseeded"

    assert listing(volume / "runs") == ["v2-a"]
    assert listing(volume / "runs" / "v2-a") == ["evaluation.json", "record.json"]
    assert "copied 1 seed runs" in caplog.text


@pytest.mark.parametrize("value", [None, "", "  "])
def test_an_unset_or_blank_target_is_refused(value):
    env = {} if value is None else {"BAZAAR_MODAL_TARGET": value}

    with pytest.raises(ValueError, match="set BAZAAR_MODAL_TARGET=live or staging"):
        rt.resolve_target(env)


def test_the_live_target_is_the_conference_app():
    target = rt.resolve_target({"BAZAAR_MODAL_TARGET": "live"})

    assert (target.app, target.volume, target.secret) == (
        "bazaar-live",
        "bazaar-live-data",
        "bazaar-live",
    )
    assert target.environment == "production"
    assert target.backup_volume == "bazaar-live-backup"
    assert dict(target.extra_env) == {}  # live keeps its caps from its secret or the defaults


def test_the_staging_target_has_its_own_app_volume_and_secret():
    target = rt.resolve_target({"BAZAAR_MODAL_TARGET": "staging"})

    assert (target.app, target.volume, target.secret) == (
        "bazaar-staging",
        "bazaar-staging-data",
        "bazaar-staging",
    )
    assert target.environment == "staging"
    assert target.backup_volume == "bazaar-staging-backup"


@pytest.mark.parametrize("value", ["prod", "Live", "STAGING", "bazaar-live", "staging2"])
def test_an_unknown_target_is_refused(value):
    with pytest.raises(ValueError, match="BAZAAR_MODAL_TARGET must be one of live, staging"):
        rt.resolve_target({"BAZAAR_MODAL_TARGET": value})


@pytest.mark.parametrize("field", ["app", "volume", "secret", "backup_volume"])
def test_staging_can_never_resolve_to_a_live_resource(monkeypatch, field):
    staging = rt.TARGETS["staging"]
    wrong = {
        "app": "bazaar-live",
        "volume": "bazaar-live-data",
        "secret": "bazaar-live",
        "backup_volume": "bazaar-live-backup",
    }[field]
    monkeypatch.setitem(rt.TARGETS, "staging", dataclasses.replace(staging, **{field: wrong}))

    with pytest.raises(ValueError, match="must not use a bazaar-live"):
        rt.resolve_target({"BAZAAR_MODAL_TARGET": "staging"})


def test_modal_app_takes_every_live_name_from_the_target():
    source = (Path(rt.__file__).parent / "modal_app.py").read_text()

    assert '"bazaar-live' not in source
    for name in (
        "TARGET.app",
        "TARGET.volume",
        "TARGET.secret",
        "TARGET.backup_volume",
        '"BAZAAR_ENVIRONMENT": TARGET.environment',
        '"BAZAAR_MODAL_TARGET": TARGET.name',
        "**TARGET.extra_env",
    ):
        assert name in source


def test_resources_default_to_two_cores_and_4096_mib():
    assert rt.resolve_resources({}) == (2.0, 4096)
    assert rt.resolve_resources({"BAZAAR_MODAL_CPU": " ", "BAZAAR_MODAL_MEMORY": ""}) == (2.0, 4096)


def test_resources_come_from_the_environment():
    env = {"BAZAAR_MODAL_CPU": "3.5", "BAZAAR_MODAL_MEMORY": "8192"}

    assert rt.resolve_resources(env) == (3.5, 8192)


@pytest.mark.parametrize("cpu", ["two", "0", "-1", "nan", "inf"])
def test_a_bad_cpu_value_is_refused(cpu):
    with pytest.raises(ValueError, match="BAZAAR_MODAL_CPU"):
        rt.resolve_resources({"BAZAAR_MODAL_CPU": cpu})


@pytest.mark.parametrize("memory", ["4GiB", "4096.0", "0", "-1", "1e4"])
def test_a_bad_memory_value_is_refused(memory):
    with pytest.raises(ValueError, match="BAZAAR_MODAL_MEMORY"):
        rt.resolve_resources({"BAZAAR_MODAL_MEMORY": memory})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BAZAAR_MAX_PER_IP_PER_HOUR", "1000"),
        ("BAZAAR_MAX_SUBMISSIONS_PER_DAY", "1000"),
        ("BAZAAR_MAX_QUEUE", "100"),
    ],
)
def test_each_staging_cap_has_the_value_replay_specified(name, value):
    staging = rt.resolve_target({"BAZAAR_MODAL_TARGET": "staging"})

    assert staging.extra_env[name] == value
    assert len(staging.extra_env) == 3
    assert name not in rt.resolve_target({"BAZAAR_MODAL_TARGET": "live"}).extra_env


def test_the_market_keeps_idle_connections_open_longer_than_clients_do(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(rt.subprocess, "Popen", lambda args, env: started.append(args))

    rt.start_market(tmp_path / "market.sqlite3", port=8123, env={"BAZAAR_RUNNER_TOKEN": TOKEN})

    [args] = started
    assert args[args.index("--timeout-keep-alive") + 1] == "75"
    assert rt.MARKET_KEEP_ALIVE > 5  # httpx's default keepalive_expiry
    assert args[args.index("--port") + 1] == "8123"


@pytest.mark.parametrize("name", ["live", "staging"])
@pytest.mark.parametrize("backup", ["", "same"])
def test_every_target_needs_its_own_backup_volume(monkeypatch, name, backup):
    target = rt.TARGETS[name]
    wrong = target.volume if backup == "same" else ""
    monkeypatch.setitem(rt.TARGETS, name, dataclasses.replace(target, backup_volume=wrong))

    with pytest.raises(ValueError, match="needs a backup volume apart from its data volume"):
        rt.resolve_target({"BAZAAR_MODAL_TARGET": name})


def test_modal_app_mounts_the_backup_volume_and_wires_backups_and_saves():
    source = (Path(rt.__file__).parent / "modal_app.py").read_text()

    assert "modal.Volume.from_name(TARGET.backup_volume, create_if_missing=True)" in source
    assert "volumes={DATA: volume, BACKUP: backup_volume}" in source
    assert "live_runtime.WebBackups(" in source and "backup_volume.commit" in source
    assert "backups=self.backups" in source
    assert "web_app.state.on_saved = self.committer.on_saved" in source


# ---------- the debounced commit after a new lead or submission ----------


def test_on_saved_returns_at_once_and_commits_soon_after():
    gate = threading.Event()
    commits = Commits()

    def slow_commit():
        gate.wait(5)
        commits()

    committer = rt.VolumeCommitter(slow_commit, interval=3600, debounce=0.05).start()
    started = time.monotonic()
    committer.on_saved()
    assert time.monotonic() - started < 0.05  # the commit is blocked; the hook is not
    gate.set()
    assert wait_until(lambda: commits.count == 1)
    committer.stop()


def test_a_burst_of_saves_is_one_commit_per_debounce_window():
    commits = Commits()
    committer = rt.VolumeCommitter(commits, interval=3600, debounce=0.5).start()
    committer.on_saved()
    assert wait_until(lambda: commits.count == 1)
    for _ in range(50):
        committer.on_saved()
    time.sleep(0.2)
    assert commits.count == 1  # still inside the window
    assert wait_until(lambda: commits.count == 2)  # the burst, once
    time.sleep(0.7)
    committer.stop()

    assert commits.count == 3  # plus the shutdown commit


def test_a_failed_saved_commit_is_logged_and_the_thread_lives_on(caplog):
    commits = Commits(fail=True)
    committer = rt.VolumeCommitter(commits, interval=3600, debounce=0.01).start()
    with caplog.at_level("ERROR", logger="bazaar.live"):
        committer.on_saved()
        assert wait_until(lambda: commits.count == 1)
        committer.on_saved()
        assert wait_until(lambda: commits.count == 2)
    committer.stop()

    assert "volume commit failed (new row)" in caplog.text


# ---------- web database backups ----------


LEAD_EMAIL = "zz-backup-sentinel@example.com"


def web_db(path: Path) -> Path:
    """A web database in WAL mode holding a lead and a submission, as the app writes them."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            "CREATE TABLE leads (id TEXT PRIMARY KEY, email TEXT UNIQUE, created_at TEXT);"
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT, email TEXT);"
        )
        conn.execute(
            "INSERT INTO leads VALUES ('l1', ?, '2026-10-12T19:00:00+00:00')", (LEAD_EMAIL,)
        )
        conn.execute("INSERT INTO submissions VALUES ('s1', 'alice-bot', 'alice@example.com')")
        conn.commit()
    return path


def rows(db: Path) -> tuple[list, list]:
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return (
            conn.execute("SELECT id, email FROM leads").fetchall(),
            conn.execute("SELECT id, name FROM submissions").fetchall(),
        )


NOW = datetime(2026, 10, 12, 19, 30, 5, tzinfo=UTC)


def test_a_backup_is_an_online_copy_with_every_committed_row(tmp_path):
    db = web_db(tmp_path / "web.sqlite3")
    # A committed row still only in the WAL file: a plain file copy of web.sqlite3 would miss it.
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO submissions VALUES ('s2', 'late-bot', 'late@example.com')")
        conn.commit()
        copy = rt.backup_web_db(db, tmp_path / "backup", now=NOW)

    assert copy == tmp_path / "backup" / "web-20261012T193005Z.sqlite3"
    assert rows(copy) == ([("l1", LEAD_EMAIL)], [("s1", "alice-bot"), ("s2", "late-bot")])
    assert sorted(p.name for p in (tmp_path / "backup").iterdir()) == [copy.name]  # no -wal, .part


def test_two_backups_in_one_second_both_keep(tmp_path):
    db = web_db(tmp_path / "web.sqlite3")
    first = rt.backup_web_db(db, tmp_path / "backup", now=NOW)
    second = rt.backup_web_db(db, tmp_path / "backup", now=NOW)

    assert (first.name, second.name) == (
        "web-20261012T193005Z.sqlite3",
        "web-20261012T193005Z-2.sqlite3",
    )
    assert rt.backups(tmp_path / "backup") == [first, second]


def test_retention_keeps_exactly_the_newest_48(tmp_path):
    db = web_db(tmp_path / "web.sqlite3")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "notes.txt").write_text("not a backup")  # never deleted
    made = [rt.backup_web_db(db, backup, now=NOW + timedelta(minutes=15 * i)) for i in range(52)]

    kept = rt.backups(backup)
    assert len(kept) == 48 and kept == made[4:]
    assert (backup / "notes.txt").exists()
    assert len(list(backup.iterdir())) == 49


def test_a_backup_while_a_writer_holds_begin_immediate_succeeds(tmp_path):
    db = web_db(tmp_path / "web.sqlite3")
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO submissions VALUES ('s3', 'uncommitted-bot', 'u@example.com')")
    try:
        copy = rt.backup_web_db(db, tmp_path / "backup", now=NOW, attempts=1)
    finally:
        writer.execute("COMMIT")
        writer.close()

    assert copy is not None
    assert rows(copy) == ([("l1", LEAD_EMAIL)], [("s1", "alice-bot")])  # the committed snapshot


def test_a_backup_that_keeps_failing_is_logged_by_class_and_returns_none(
    tmp_path, caplog, monkeypatch
):
    db = web_db(tmp_path / "web.sqlite3")

    def locked(self, target, **kwargs):
        raise sqlite3.OperationalError(f"database is locked near {LEAD_EMAIL}")

    monkeypatch.setattr(rt.sqlite3, "connect", _connect_with(locked))
    with caplog.at_level("WARNING", logger="bazaar.live"):
        copy = rt.backup_web_db(db, tmp_path / "backup", now=NOW, attempts=2, retry_wait=0)

    assert copy is None
    assert "web backup attempt 2 of 2 failed (OperationalError)" in caplog.text
    assert LEAD_EMAIL not in caplog.text
    assert list((tmp_path / "backup").iterdir()) == []  # no .part left behind


def test_a_locked_first_attempt_is_retried(tmp_path, monkeypatch):
    db = web_db(tmp_path / "web.sqlite3")
    calls = []

    def flaky(self, target, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return self.backup_real(target)

    monkeypatch.setattr(rt.sqlite3, "connect", _connect_with(flaky))
    copy = rt.backup_web_db(db, tmp_path / "backup", now=NOW, retry_wait=0)
    monkeypatch.undo()  # rt.sqlite3 is the sqlite3 module: rows() needs the real connect

    assert len(calls) == 2 and rows(copy)[1] == [("s1", "alice-bot")]


def _connect_with(backup):
    """sqlite3.connect whose connections run `backup` instead of Connection.backup."""
    real = sqlite3.connect

    class Conn:
        def __init__(self, *args, **kwargs):
            self.conn = real(*args, **kwargs)

        def backup(self, target, **kwargs):
            return backup(self, target, **kwargs)

        def backup_real(self, target):
            return self.conn.backup(target.conn)

        def close(self):
            self.conn.close()

    return Conn


def test_no_database_means_no_backup_and_no_empty_file(tmp_path):
    assert rt.backup_web_db(tmp_path / "missing.sqlite3", tmp_path / "backup", now=NOW) is None
    assert not (tmp_path / "missing.sqlite3").exists()


def test_web_backups_commit_the_backup_volume_after_each_copy(tmp_path):
    db = web_db(tmp_path / "web.sqlite3")
    seen = []

    def commit():
        seen.append(len(rt.backups(tmp_path / "backup")))

    backups = rt.WebBackups(db, tmp_path / "backup", commit, interval=0.05).start()
    assert wait_until(lambda: len(seen) >= 2)
    backups.stop()  # and one last backup

    assert seen[:2] == [1, 2] and len(rt.backups(tmp_path / "backup")) == len(seen)


def test_a_failed_backup_volume_commit_is_logged_not_raised(tmp_path, caplog):
    db = web_db(tmp_path / "web.sqlite3")
    backups = rt.WebBackups(db, tmp_path / "backup", Commits(fail=True), interval=3600)

    with caplog.at_level("WARNING", logger="bazaar.live"):
        assert backups.backup_now("test") is False

    assert "backup volume commit failed (test, RuntimeError)" in caplog.text


def test_shutdown_backs_up_before_the_last_data_commit(tmp_path):
    order = []
    db = web_db(tmp_path / "web.sqlite3")
    committer = rt.VolumeCommitter(lambda: order.append("data commit"), interval=3600)
    backups = rt.WebBackups(db, tmp_path / "backup", lambda: order.append("backup commit"))
    rt.shutdown(sleeper(), committer, threading.Event(), timeout=5, backups=backups)

    assert order == ["backup commit", "data commit"]
    assert len(rt.backups(tmp_path / "backup")) == 1


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
