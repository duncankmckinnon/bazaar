"""In-process FIFO queue running submissions in threads, a few at a time."""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

import logfire
from bazaar_replay.leaderboard import Run, load_run
from opentelemetry import trace

from bazaar_web.board import BoardSource, percent
from bazaar_web.settings import SECRET_ENV_VARS, Settings
from bazaar_web.store import Store

log = logging.getLogger("bazaar_web.worker")

TRACER = trace.get_tracer("bazaar-web")

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
        on_progress: Callable[..., None] | None = None,
    ) -> Path: ...


def expected_run_dir(runs_dir: Path, submission_id: str) -> Path:
    """Where run_submission puts this submission's finished run.

    Mirrors C2 on conf/runner-submission: bazaar_runner/submission.py:53 (experiment_id) and
    bazaar_runner/demo.py:100 (run_id = uuid5(experiment_id, "run")); the dir is runs_dir/run_id.
    """
    experiment_id = uuid5(NAMESPACE_URL, f"bazaar:sub-{submission_id}")
    return runs_dir / str(uuid5(experiment_id, "run"))


def carrier_of(submission: dict[str, Any]) -> dict[str, str]:
    """The trace context saved when the submission was posted ({} if none)."""
    try:
        return json.loads(submission.get("trace_context") or "{}")
    except ValueError:
        return {}


def finished(run_dir: Path) -> bool:
    return (run_dir / "record.json").is_file() and (run_dir / "evaluation.json").is_file()


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

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self._recover()
        self.tasks = [
            asyncio.create_task(self._consume()) for _ in range(self.settings.max_concurrent)
        ]

    async def _recover(self) -> None:
        """Pick up after a restart without paying for a run twice.

        A run that was "running" may have finished on disk before the restart: score it from its
        run dir (a rerun would fail after the whole model run). Otherwise queue it again, ahead of
        the waiting submissions, since it had already started. Staging dirs (.tmp-*) never match
        the expected run dir.
        """
        restarted = []
        with logfire.suppress_instrumentation():
            running = self.store.ids_with_status("running")
        for submission_id in running:
            # Each recovered run keeps the trace of the request that submitted it.
            with logfire.propagate.attach_context(self._carrier(submission_id)):
                run_dir = expected_run_dir(self.settings.runs_dir, submission_id)
                if finished(run_dir):
                    await self._scored(submission_id, run_dir)
                else:
                    self.store.requeue(submission_id)
                    restarted.append(submission_id)
        with logfire.suppress_instrumentation():
            queued = self.store.ids_with_status("queued")
        waiting = [i for i in queued if i not in restarted]
        for submission_id in restarted + waiting:
            self.queue.put_nowait(submission_id)

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

    def _carrier(self, submission_id: str) -> dict[str, str]:
        with logfire.suppress_instrumentation():
            submission = self.store.get(submission_id)
        return carrier_of(submission) if submission else {}

    async def _run(self, submission_id: str) -> None:
        with logfire.suppress_instrumentation():
            submission = self.store.get(submission_id)
        if submission is None or submission["status"] != "queued":
            return
        # Everything for this job, including its store writes, joins the submission's trace.
        with logfire.propagate.attach_context(carrier_of(submission)):
            await self._run_submission(submission)

    async def _run_submission(self, submission: dict[str, Any]) -> None:
        submission_id = submission["id"]
        name = submission["name"]
        self.store.mark_running(submission_id)
        self.store.add_event(f"{name} started trading", submission_id)

        # Runners before C2's value change call on_progress(day); newer ones pass the marked value.
        def on_progress(day: int, value: Decimal | None = None) -> None:
            if self.store.set_day(submission_id, day, value):
                self.store.add_event(f"{name} is on day {day} of 10", submission_id)

        try:
            run = self.run_submission or import_run_submission()
        except ImportError as exc:
            self._fail(submission, UNAVAILABLE, exc)
            return

        def in_thread() -> Path:
            # Attach in the thread itself, so the run is a child of the submission trace.
            with logfire.propagate.attach_context(carrier_of(submission)):
                queued = TRACER.start_span(
                    "submission queued",
                    start_time=created_ns(submission["created_at"]),
                    attributes={"submission_id": submission_id},
                )
                queued.end()  # the queue wait ends as the run starts; the run nests under it
                with trace.use_span(queued, end_on_exit=False):
                    return run(
                        submission_id=submission_id,
                        name=name,
                        instructions=submission["instructions"],
                        market_url=self.settings.market_url,
                        runner_token=self.settings.runner_token,
                        runs_dir=self.settings.runs_dir,
                        on_progress=on_progress,
                    )

        try:
            run_dir = await asyncio.to_thread(in_thread)
        except Exception as exc:  # noqa: BLE001 - the runner raises on any failed run
            self._fail(submission, FAILED, exc)
            return

        await self._scored(submission_id, Path(run_dir))

    async def _scored(self, submission_id: str, run_dir: Path) -> None:
        self.store.finish(submission_id, run_dir=run_dir.name, error=None)
        self.board.invalidate()
        name = self.store.get(submission_id)["name"]
        self.store.add_event(scored_text(name, run_dir), submission_id)
        await self._notify(submission_id, run_dir)

    def _redact(self, exc: Exception) -> str:
        """The exception class and message, with any configured secret masked."""
        text = f"{type(exc).__name__}: {exc}"
        secrets = [os.environ.get(name) for name in SECRET_ENV_VARS]
        secrets += [self.settings.runner_token, self.settings.admin_token]
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
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


def created_ns(created_at: str) -> int:
    return int(datetime.fromisoformat(created_at).timestamp() * 1_000_000_000)


def scored_text(name: str, run_dir: Path) -> str:
    run = load_run(run_dir)
    if isinstance(run, Run) and run.evaluation is not None:
        value = percent(run.evaluation.period.period_return)
        if value is not None:
            return f"{name} finished at {value:+.2f}%"
    return f"{name} finished"
