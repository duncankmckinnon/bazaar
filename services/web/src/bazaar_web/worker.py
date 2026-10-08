"""In-process FIFO queue running submissions in threads, a few at a time."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from bazaar_replay.leaderboard import Run, load_run

from bazaar_web.board import BoardSource, percent
from bazaar_web.settings import Settings
from bazaar_web.store import Store

log = logging.getLogger("bazaar_web.worker")

FAILED = "the run failed; please try again"
UNAVAILABLE = "runner unavailable"


class RunSubmission(Protocol):
    def __call__(
        self,
        *,
        submission_id: str,
        name: str,
        instructions: str,
        market_url: str,
        runner_token: str,
        runs_dir: Path,
        model: str | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> Path: ...


def import_run_submission() -> RunSubmission:
    from bazaar_runner.submission import run_submission  # C2; not on main yet

    return run_submission


class Worker:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        board: BoardSource,
        run_submission: RunSubmission | None,
        on_scored: Callable[[], Callable[[str, Path], None] | None],
    ) -> None:
        self.store = store
        self.settings = settings
        self.board = board
        self.run_submission = run_submission
        self.on_scored = on_scored
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        for submission_id in self.store.recover():
            self.queue.put_nowait(submission_id)
        self.tasks = [
            asyncio.create_task(self._consume()) for _ in range(self.settings.max_concurrent)
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def enqueue(self, submission_id: str) -> None:
        """Safe from the threadpool that runs sync routes."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, submission_id)

    async def _consume(self) -> None:
        while True:
            submission_id = await self.queue.get()
            try:
                await self._run(submission_id)
            except Exception:
                log.exception("worker error on submission %s", submission_id)
            finally:
                self.queue.task_done()

    async def _run(self, submission_id: str) -> None:
        submission = self.store.get(submission_id)
        if submission is None or submission["status"] != "queued":
            return
        name = submission["name"]
        self.store.mark_running(submission_id)
        self.store.add_event(f"{name} started trading", submission_id)

        def on_progress(day: int) -> None:
            if self.store.set_day(submission_id, day):
                self.store.add_event(f"{name} is on day {day} of 10", submission_id)

        try:
            run = self.run_submission or import_run_submission()
        except ImportError as exc:
            self._fail(submission, UNAVAILABLE, exc)
            return
        try:
            run_dir = await asyncio.to_thread(
                run,
                submission_id=submission_id,
                name=name,
                instructions=submission["instructions"],
                market_url=self.settings.market_url,
                runner_token=self.settings.runner_token,
                runs_dir=self.settings.runs_dir,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001 - the runner raises on any failed run
            self._fail(submission, FAILED, exc)
            return

        run_dir = Path(run_dir)
        self.store.finish(submission_id, run_dir=run_dir.name, error=None)
        self.board.invalidate()
        self.store.add_event(scored_text(name, run_dir), submission_id)
        await self._notify(submission_id, run_dir)

    def _redact(self, exc: Exception) -> str:
        """The exception class and message, with any configured secret masked."""
        text = f"{type(exc).__name__}: {exc}"
        for secret in (self.settings.runner_token, self.settings.admin_token):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _fail(self, submission: dict[str, Any], error: str, exc: Exception) -> None:
        log.warning("submission %s failed: %s", submission["id"], self._redact(exc))
        self.store.finish(submission["id"], run_dir=None, error=error)
        self.store.add_event(f"{submission['name']}'s run failed", submission["id"])

    async def _notify(self, submission_id: str, run_dir: Path) -> None:
        hook = self.on_scored()
        if hook is None:
            return
        try:
            await asyncio.to_thread(hook, submission_id, run_dir)
        except Exception as exc:  # noqa: BLE001 - a hook must never change the status
            log.warning("on_scored hook failed for %s: %s", submission_id, self._redact(exc))


def scored_text(name: str, run_dir: Path) -> str:
    run = load_run(run_dir)
    if isinstance(run, Run) and run.evaluation is not None:
        value = percent(run.evaluation.period.period_return)
        if value is not None:
            return f"{name} finished at {value:+.2f}%"
    return f"{name} finished"
