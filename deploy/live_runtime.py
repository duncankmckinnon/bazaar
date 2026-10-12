"""Runtime pieces of the live deploy, kept free of Modal so they can be tested offline.

`modal_app.py` calls these from its container hooks: check the runner token, copy the seed data
onto the volume, start the market as a subprocess and wait for it, commit the volume on a
timer, after each scored run and soon after each new lead or submission, and back up the web
database to a second volume.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("bazaar.live")

MARKET_PORT = 8000
# Idle keep-alive seconds for the market's uvicorn, well above any client's keepalive_expiry
# (httpx: 5 s). At uvicorn's default of 5 s a client could reuse a connection the server was closing.
MARKET_KEEP_ALIVE = 75
COMMIT_INTERVAL = 10.0
# A new lead or submission asks for a commit at once, then at most one per this many seconds.
SAVED_DEBOUNCE = 2.0
BACKUP_INTERVAL = 15 * 60.0
BACKUPS_KEPT = 48  # 12 hours at one every 15 minutes
BACKUP_NAME = re.compile(r"web-(\d{8}T\d{6}Z)(?:-(\d+))?\.sqlite3")
LIVE_PREFIX = "bazaar-live"
DEFAULT_CPU = 2.0
DEFAULT_MEMORY_MIB = 4096


@dataclass(frozen=True)
class ModalTarget:
    """The Modal app, volume and secret one deploy uses, its Logfire environment and extra env."""

    name: str
    app: str
    volume: str
    secret: str
    environment: str
    # Container env only this target sets; live keeps its caps from its secret or the web defaults.
    extra_env: Mapping[str, str] = field(default_factory=dict)
    # The volume the web database is backed up to (`WebBackups`), apart from the data volume.
    backup_volume: str = ""


# Load tests submit far more than the conference caps allow.
STAGING_CAPS = {
    "BAZAAR_MAX_QUEUE": "100",
    "BAZAAR_MAX_SUBMISSIONS_PER_DAY": "1000",
    # One load-test IP submits everything, so a low per-IP cap would read as a throughput limit.
    "BAZAAR_MAX_PER_IP_PER_HOUR": "1000",
}
TARGETS = {
    "live": ModalTarget(
        "live",
        "bazaar-live",
        "bazaar-live-data",
        "bazaar-live",
        "production",
        backup_volume="bazaar-live-backup",
    ),
    "staging": ModalTarget(
        "staging",
        "bazaar-staging",
        "bazaar-staging-data",
        "bazaar-staging",
        "staging",
        STAGING_CAPS,
        backup_volume="bazaar-staging-backup",
    ),
}


def resolve_target(env: Mapping[str, str] = os.environ) -> ModalTarget:
    """The deploy target from BAZAAR_MODAL_TARGET: "live" or "staging", with no default.

    Unset, blank or any other value raises, so a staging deploy that loses the variable cannot
    land on the conference app. Only the live target may touch a bazaar-live app, volume (data or
    backup) or secret, so a load test on staging can never write to the conference board, its
    backups or use its secret. Every target needs its own backup volume.
    """
    name = env.get("BAZAAR_MODAL_TARGET", "").strip()
    if not name:
        raise ValueError(f"set BAZAAR_MODAL_TARGET={' or '.join(TARGETS)}; there is no default")
    target = TARGETS.get(name)
    if target is None:
        raise ValueError(f"BAZAAR_MODAL_TARGET must be one of {', '.join(TARGETS)}, not {name!r}")
    if not target.backup_volume or target.backup_volume == target.volume:
        raise ValueError(
            f"the {target.name} target needs a backup volume apart from its data volume"
        )
    if target.name != "live" and any(
        value.startswith(LIVE_PREFIX)
        for value in (target.app, target.volume, target.secret, target.backup_volume)
    ):
        raise ValueError(
            f"the {target.name} target must not use a {LIVE_PREFIX} app, volume or secret"
        )
    return target


def resolve_resources(env: Mapping[str, str] = os.environ) -> tuple[float, int]:
    """CPU cores and memory in MiB from BAZAAR_MODAL_CPU and BAZAAR_MODAL_MEMORY.

    Defaults are 2.0 cores and 4096 MiB. A value that is not a positive number raises rather than
    deploy something unasked; Modal itself enforces its own upper limits.
    """
    raw_cpu = env.get("BAZAAR_MODAL_CPU", "").strip()
    raw_memory = env.get("BAZAAR_MODAL_MEMORY", "").strip()
    try:
        cpu = float(raw_cpu) if raw_cpu else DEFAULT_CPU
    except ValueError:
        raise ValueError(f"BAZAAR_MODAL_CPU must be a number of cores, not {raw_cpu!r}") from None
    if not (math.isfinite(cpu) and cpu > 0):
        raise ValueError(f"BAZAAR_MODAL_CPU must be a positive number of cores, not {raw_cpu!r}")
    if raw_memory and not raw_memory.isdigit():
        raise ValueError(f"BAZAAR_MODAL_MEMORY must be whole MiB, not {raw_memory!r}")
    memory = int(raw_memory) if raw_memory else DEFAULT_MEMORY_MIB
    if memory <= 0:
        raise ValueError(
            f"BAZAAR_MODAL_MEMORY must be a positive number of MiB, not {raw_memory!r}"
        )
    return cpu, memory


class MissingRunnerToken(RuntimeError):
    """The market's control routes need BAZAAR_RUNNER_TOKEN; without it nothing can run."""


class MarketNotReady(RuntimeError):
    """The market subprocess exited or did not answer /health in time."""


def require_runner_token(env: Mapping[str, str] = os.environ) -> None:
    """Fail fast without BAZAAR_RUNNER_TOKEN. The message never includes any value."""
    if not env.get("BAZAAR_RUNNER_TOKEN"):
        raise MissingRunnerToken("BAZAAR_RUNNER_TOKEN is not set; refusing to start")


def prepare_data(volume_dir: Path, seed_db: Path, seed_runs: Path | None) -> tuple[Path, Path]:
    """Give the volume a writable market DB and the seed runs, without overwriting either.

    Logs which database is in use and whether it was just seeded.

    Returns the database path and the runs directory. A missing seed database with no database
    on the volume is an error; missing seed runs only leave the runs directory empty.
    """
    volume_dir = Path(volume_dir)
    db, runs = volume_dir / "market.sqlite3", volume_dir / "runs"
    volume_dir.mkdir(parents=True, exist_ok=True)
    if db.exists():
        # A new seed never replaces a database the volume already has. See deploy/README.md.
        logger.info("market database in use: %s (already on the volume; seed not applied)", db)
    else:
        if not Path(seed_db).is_file():
            raise FileNotFoundError(f"no market database at {db} and no seed at {seed_db}")
        partial = db.with_name(db.name + ".part")
        shutil.copyfile(seed_db, partial)
        os.replace(partial, db)
        logger.info("market database in use: %s (freshly seeded from %s)", db, seed_db)
    if not runs.exists():
        if seed_runs is not None and Path(seed_runs).is_dir():
            partial = runs.with_name("runs.part")
            shutil.rmtree(partial, ignore_errors=True)
            shutil.copytree(seed_runs, partial)
            os.replace(partial, runs)
            logger.info("copied the seed runs to %s", runs)
        else:
            runs.mkdir()
    return db, runs


def start_market(
    db_path: Path,
    *,
    port: int = MARKET_PORT,
    env: Mapping[str, str] = os.environ,
    python: str = sys.executable,
) -> subprocess.Popen:
    """Start the market on 127.0.0.1:`port` against `db_path`. Call `wait_healthy` next."""
    require_runner_token(env)
    child_env = {**env, "BAZAAR_MARKET_DB": str(db_path)}
    command = [python, "-m", "uvicorn", "bazaar_market.app:app", "--host", "127.0.0.1"]
    command += ["--port", str(port), "--timeout-keep-alive", str(MARKET_KEEP_ALIVE)]
    return subprocess.Popen(command, env=child_env)


def wait_healthy(url: str, timeout: float = 30.0, process: subprocess.Popen | None = None) -> None:
    """Return once `url` answers 200. Raise `MarketNotReady` on timeout or if `process` exits."""
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise MarketNotReady(f"the market exited with status {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise MarketNotReady(f"{url} did not answer within {timeout:g}s")
        time.sleep(0.25)


class VolumeCommitter:
    """Commits the data volume on a timer and on demand, one commit at a time.

    `on_scored(submission_id, run_dir)` is the hook the web app calls after each scored run.
    A failed commit is logged and never raised, so it cannot fail a web request.
    """

    def __init__(
        self,
        commit: Callable[[], None],
        interval: float = COMMIT_INTERVAL,
        debounce: float = SAVED_DEBOUNCE,
    ) -> None:
        self._commit = commit
        self._interval = interval
        self._debounce = debounce
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._wanted = threading.Event()
        self._thread: threading.Thread | None = None
        self._saver: threading.Thread | None = None

    def commit_now(self, reason: str = "timer") -> bool:
        with self._lock:
            try:
                self._commit()
            except Exception:
                logger.exception("volume commit failed (%s)", reason)
                return False
        return True

    def on_scored(self, submission_id: str, run_dir: object) -> None:
        self.commit_now(f"scored {submission_id}")

    def on_saved(self) -> None:
        """The web app's hook after a new lead or submission has committed to SQLite: ask the
        saver thread for a commit and return at once. The thread commits right away, then at
        most once per `SAVED_DEBOUNCE` seconds however many rows arrive."""
        self._wanted.set()

    def start(self) -> VolumeCommitter:
        def loop() -> None:
            while not self._stopped.wait(self._interval):
                self.commit_now()

        def saver() -> None:
            while not self._stopped.is_set():
                if not self._wanted.wait(timeout=1.0):
                    continue
                self._wanted.clear()
                self.commit_now("new row")
                self._stopped.wait(self._debounce)

        self._thread = threading.Thread(target=loop, name="volume-commit", daemon=True)
        self._thread.start()
        self._saver = threading.Thread(target=saver, name="volume-commit-saved", daemon=True)
        self._saver.start()
        return self

    def stop(self) -> None:
        """Stop the timer and make one last commit."""
        self._stopped.set()
        for thread in (self._thread, self._saver):
            if thread is not None:
                thread.join(timeout=self._interval + 5)
        self.commit_now("shutdown")


def backup_name(now: datetime) -> str:
    return f"web-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"


def backups(backup_dir: Path) -> list[Path]:
    """The backup files in `backup_dir`, oldest first; anything else there is ignored."""
    found = []
    for path in Path(backup_dir).glob("web-*.sqlite3"):
        match = BACKUP_NAME.fullmatch(path.name)
        if match:
            found.append(((match[1], int(match[2] or 0)), path))
    return [path for _, path in sorted(found)]


def backup_web_db(
    db: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
    keep: int = BACKUPS_KEPT,
    attempts: int = 3,
    retry_wait: float = 1.0,
) -> Path | None:
    """Copy the web database with SQLite's online backup API, never a file copy, to
    <backup_dir>/web-<UTC stamp>.sqlite3, then delete all but the newest `keep` backups.

    The copy is a consistent snapshot even while the app writes. It goes to a .part file that is
    renamed when complete, so a backup file is never half written. A busy or locked database is
    retried. Returns the new file, or None after logging the failure (by exception class only:
    the database holds contact details). Never raises.
    """
    db, backup_dir = Path(db), Path(backup_dir)
    if not db.is_file():  # connecting would create an empty database
        logger.warning("web backup skipped: no database at %s", db)
        return None
    name = backup_name(now or datetime.now(UTC))
    target = backup_dir / name
    suffix = 1
    while target.exists():
        suffix += 1
        target = backup_dir / name.replace(".sqlite3", f"-{suffix}.sqlite3")
    partial = target.with_name(target.name + ".part")
    for attempt in range(1, attempts + 1):
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            partial.unlink(missing_ok=True)
            source = sqlite3.connect(db, timeout=10)
            try:
                destination = sqlite3.connect(partial)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            os.replace(partial, target)
            break
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "web backup attempt %d of %d failed (%s)", attempt, attempts, type(exc).__name__
            )
            if attempt == attempts:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            time.sleep(retry_wait)
    logger.info("web database backed up to %s", target)
    for old in backups(backup_dir)[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("could not delete old backup %s (%s)", old.name, type(exc).__name__)
    return target


class WebBackups:
    """Backs up the web database every `interval` seconds and once at `stop`, committing the
    backup volume after each copy. Failures are logged and never raised."""

    def __init__(
        self,
        db: Path,
        backup_dir: Path,
        commit: Callable[[], None],
        interval: float = BACKUP_INTERVAL,
        keep: int = BACKUPS_KEPT,
    ) -> None:
        self._db, self._dir, self._commit = Path(db), Path(backup_dir), commit
        self._interval, self._keep = interval, keep
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def backup_now(self, reason: str = "timer") -> bool:
        with self._lock:
            if backup_web_db(self._db, self._dir, keep=self._keep) is None:
                return False
            try:
                self._commit()
            except Exception as exc:  # noqa: BLE001 - logged, never raised
                logger.warning("backup volume commit failed (%s, %s)", reason, type(exc).__name__)
                return False
        return True

    def start(self) -> WebBackups:
        def loop() -> None:
            while not self._stopped.wait(self._interval):
                self.backup_now()

        self._thread = threading.Thread(target=loop, name="web-backup", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the timer and take one last backup."""
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
        self.backup_now("shutdown")


def watch_market(
    process: subprocess.Popen,
    stopping: threading.Event,
    exit: Callable[[int], object] = os._exit,
    before_exit: Callable[[], object] | None = None,
) -> threading.Thread:
    """Exit the whole container if the market dies on its own, so the platform restarts it.

    Without this, every run would fail against a dead market while the web app looked healthy.
    `before_exit`, usually a last volume commit, runs first; a failure in it is logged and the
    container still exits. An exit during `shutdown` is expected and ignored.
    """

    def watch() -> None:
        status = process.wait()
        if stopping.is_set():
            return
        logger.error("the market exited with status %s; stopping the container", status)
        if before_exit is not None:
            try:
                before_exit()
            except Exception:
                logger.exception("the last commit before exiting failed")
        exit(1)

    thread = threading.Thread(target=watch, name="market-watchdog", daemon=True)
    thread.start()
    return thread


def shutdown(
    process: subprocess.Popen,
    committer: VolumeCommitter,
    stopping: threading.Event,
    timeout: float = 10.0,
    backups: WebBackups | None = None,
) -> None:
    """Stop the market, wait until it has exited, take the last web backup, then make the
    last data volume commit."""
    stopping.set()
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if backups is not None:
        backups.stop()
    committer.stop()


RESEED_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
RUN_FILES = ("record.json", "evaluation.json")


def valid_seed_runs(seed_runs: Path, reseed_id: str) -> list[Path]:
    """The seed's run folders, or raise before anything moves.

    A run is a folder holding record.json and evaluation.json. Dotfiles such as .DS_Store are
    ignored. Any other entry is an error, and so is a seed with no runs at all.
    """
    entries = sorted(seed_runs.iterdir()) if seed_runs.is_dir() else []
    entries = [entry for entry in entries if not entry.name.startswith(".")]
    invalid = [
        entry.name for entry in entries if not all((entry / name).is_file() for name in RUN_FILES)
    ]
    if invalid:
        raise FileNotFoundError(
            f"reseed {reseed_id}: not runs (need {' and '.join(RUN_FILES)}): "
            f"{', '.join(invalid)} in {seed_runs}; nothing moved"
        )
    if not entries:
        raise FileNotFoundError(f"reseed {reseed_id}: no seed runs at {seed_runs}; nothing moved")
    return entries


def reseed_runs(
    volume_dir: Path,
    seed_runs: Path,
    reseed_id: str | None,
    now: datetime | None = None,
    commit: Callable[[], object] | None = None,
) -> str:
    """Replace the board's runs with a new seed once per `reseed_id`, archiving the old ones.

    Off unless `reseed_id` is non-empty. The flag stays set for the whole deploy and the
    container restarts after a market crash, so a marker on the volume makes it one-shot: the
    same id never reseeds twice, or it would archive attendee runs scored since. Nothing is
    deleted; the old runs move to <volume>/archive/<utc time>/runs. Missing, empty or invalid
    seed runs raise before anything moves (see `valid_seed_runs`). Returns "off", "skipped" or "reseeded".
    """
    if not reseed_id or not reseed_id.strip():
        return "off"
    if not RESEED_ID.fullmatch(reseed_id):
        raise ValueError("BAZAAR_RESEED_RUNS must be 1-64 letters, digits, '.', '_' or '-'")
    volume_dir = Path(volume_dir)
    archive = volume_dir / "archive"
    marker = archive / f"reseeded-{reseed_id}"
    if marker.exists():
        logger.info("reseed %s skipped: already done (%s exists)", reseed_id, marker)
        return "skipped"
    seed_runs = Path(seed_runs)
    entries = valid_seed_runs(seed_runs, reseed_id)

    runs = volume_dir / "runs"
    # Copy first: if the copy fails, the board's runs have not moved.
    partial = volume_dir / "runs.part"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(seed_runs, partial, ignore=shutil.ignore_patterns(".*"))
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = archive / stamp
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = archive / f"{stamp}-{suffix}"
    if runs.exists():
        destination.mkdir(parents=True)
        os.replace(runs, destination / "runs")
        logger.info("reseed %s: archived the previous runs to %s", reseed_id, destination / "runs")
    os.replace(partial, runs)
    logger.info("reseed %s: copied %d seed runs into %s", reseed_id, len(entries), runs)

    archive.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{stamp}\n")
    if commit is not None:
        try:
            commit()
        except Exception:
            logger.exception("reseed %s: volume commit failed; the timer will retry", reseed_id)
    return "reseeded"
