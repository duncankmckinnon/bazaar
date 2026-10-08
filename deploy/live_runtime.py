"""Runtime pieces of the live deploy, kept free of Modal so they can be tested offline.

`modal_app.py` calls these from its container hooks: check the runner token, copy the seed data
onto the volume, start the market as a subprocess and wait for it, and commit the volume on a
timer and after each scored run.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
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

    Returns the database path and the runs directory. A missing seed database with no database
    on the volume is an error; missing seed runs only leave the runs directory empty.
    """
    volume_dir = Path(volume_dir)
    db, runs = volume_dir / "market.sqlite3", volume_dir / "runs"
    volume_dir.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        if not Path(seed_db).is_file():
            raise FileNotFoundError(f"no market database at {db} and no seed at {seed_db}")
        partial = db.with_name(db.name + ".part")
        shutil.copyfile(seed_db, partial)
        os.replace(partial, db)
        logger.info("copied the seed market database to %s", db)
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
