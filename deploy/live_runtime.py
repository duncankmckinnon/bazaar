"""Runtime pieces of the live deploy, kept free of Modal so they can be tested offline.

`modal_app.py` calls these from its container hooks: check the runner token, copy the seed data
onto the volume, start the market as a subprocess and wait for it, and commit the volume on a
timer and after each scored run.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("bazaar.live")

MARKET_PORT = 8000
COMMIT_INTERVAL = 10.0


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
    command = [python, "-m", "uvicorn", "bazaar_market.app:app"]
    return subprocess.Popen([*command, "--host", "127.0.0.1", "--port", str(port)], env=child_env)


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

    def __init__(self, commit: Callable[[], None], interval: float = COMMIT_INTERVAL) -> None:
        self._commit = commit
        self._interval = interval
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

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

    def start(self) -> VolumeCommitter:
        def loop() -> None:
            while not self._stopped.wait(self._interval):
                self.commit_now()

        self._thread = threading.Thread(target=loop, name="volume-commit", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the timer and make one last commit."""
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
        self.commit_now("shutdown")


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
) -> None:
    """Stop the market, wait until it has exited, then make the last volume commit."""
    stopping.set()
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    committer.stop()


RESEED_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


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
    deleted; the old runs move to <volume>/archive/<utc time>/runs. Missing or empty seed runs
    raise before anything moves. Returns "off", "skipped" or "reseeded".
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
    entries = sorted(seed_runs.iterdir()) if seed_runs.is_dir() else []
    if not entries:
        raise FileNotFoundError(f"reseed {reseed_id}: no seed runs at {seed_runs}; nothing moved")

    runs = volume_dir / "runs"
    # Copy first: if the copy fails, the board's runs have not moved.
    partial = volume_dir / "runs.part"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(seed_runs, partial)
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
