"""Runs submissions in threads, in two lanes that never share slots.

The human lane is a FIFO queue of attendee submissions on BAZAAR_MAX_CONCURRENT threads. The
house lane has its own BAZAAR_HOUSE_CONCURRENT threads and keeps itself full with strategies
from the strategy factory (bazaar_web.house) while it is enabled, not paused and under its
hourly cap. It waits, and never runs anything else, while the factory is not configured, the
bundle has no universe or the strategist fails.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

import logfire
from bazaar_replay.leaderboard import Run, load_run
from opentelemetry import trace

from bazaar_web.board import BoardSource, percent
from bazaar_web.bundle import Bundle, BundleSource
from bazaar_web.house import PROPOSE_K, Strategist, import_strategist, universe_of
from bazaar_web.settings import SECRET_ENV_VARS, Settings
from bazaar_web.store import Store

log = logging.getLogger("bazaar_web.worker")

TRACER = trace.get_tracer("bazaar-web")

FAILED = "the run failed; please try again"
UNAVAILABLE = "runner unavailable"
HOUSE_OFF = "the house lane is off"
# How long an idle house slot (paused, capped or off) waits before it looks again; a resume
# wakes it at once.
HOUSE_IDLE_S = 5.0
# Consecutive failed house runs back the whole lane off: 5 s, 10 s, 20 s ... up to 5 min; a
# scored run resets it, and so does an admin resume. A failing market or runner must not become a
# hot loop of failed rows and public "run failed" events: while backing off, house failures add
# no wire event, and the HOUSE_FAIL_NOTICE-th failure in a row posts one notice instead.
HOUSE_FAIL_BACKOFF_S = 5.0
HOUSE_FAIL_BACKOFF_MAX_S = 300.0
HOUSE_FAIL_NOTICE = 5
# Why the house lane is waiting, for the admin status.
FACTORY_OFF = "factory not configured"
WAITING_FOR_BUNDLE = "waiting for bundle"
NO_UNIVERSE = "no universe in the bundle"
WAITING_FOR_STRATEGIST = "waiting for strategist"


class NoProposals(Exception):
    """The strategist answered, but no proposal survived the lane's own checks."""


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
        bundles: BundleSource | None = None,
        strategist: Strategist | None = None,
    ) -> None:
        self.store = store
        # The factory proposes over the deploy's bundle universe; without a source (only
        # direct test setups) the lane waits for a bundle.
        self.bundles = bundles
        self.strategist = strategist  # None: bazaar_agent.strategist, imported when first needed
        self.house_waiting: str | None = None  # why the lane can't get new strategies, if so
        self.propose_failures = 0  # consecutive failed strategist calls
        self.propose_retry_at: datetime | None = None  # wall time, for the admin status
        self.last_propose_at: datetime | None = None
        self.last_propose_error: str | None = None  # the exception's class name only
        self._next_propose = 0.0  # loop time
        self.settings = settings
        self.board = board
        self.run_submission = run_submission
        self.on_scored = on_scored
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.tasks: list[asyncio.Task[None]] = []
        # Runs get their own threads: the loop's default executor holds only
        # min(32, cpus + 4), and runs past that would show "running" while they wait for one.
        self.pool = ThreadPoolExecutor(settings.max_concurrent, thread_name_prefix="bazaar-run")
        # The house lane's own threads: a house run never takes a human slot, or the reverse.
        self.house_slots = settings.house_concurrent if settings.house_enabled else 0
        self.house_pool = (
            ThreadPoolExecutor(self.house_slots, thread_name_prefix="bazaar-house")
            if self.house_slots
            else None
        )
        # Strategist calls get one thread of their own: they must never wait for a run slot.
        self.propose_pool = (
            ThreadPoolExecutor(1, thread_name_prefix="bazaar-propose") if self.house_slots else None
        )
        self.house_resume: list[str] = []  # house runs interrupted by a restart, run first
        self.house_failures = 0  # consecutive failed house runs, lane-wide
        self.house_backoff_until: datetime | None = None  # wall time, for the admin status
        self._house_backoff_deadline = 0.0  # loop time

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.house_wake = asyncio.Event()
        self.propose_lock = asyncio.Lock()
        await self._recover()
        await self._recover_house()
        self.tasks = [
            asyncio.create_task(self._consume()) for _ in range(self.settings.max_concurrent)
        ]
        self.tasks += [asyncio.create_task(self._house_slot()) for _ in range(self.house_slots)]

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

    async def _recover_house(self) -> None:
        """House runs cut off by a restart: score a finished one, else run it again in the house
        lane (never among the humans). With the lane off, they end as failed."""
        with logfire.suppress_instrumentation():
            interrupted = self.store.ids_with_status("running", lane="house")
            queued = self.store.ids_with_status("queued", lane="house")
        for submission_id in interrupted + queued:
            run_dir = expected_run_dir(self.settings.runs_dir, submission_id)
            if finished(run_dir):
                await self._scored(submission_id, run_dir)
            elif self.house_pool is None:
                self.store.finish(
                    submission_id, run_dir=None, error=HOUSE_OFF, error_class="HouseLaneOff"
                )
            else:
                self.store.requeue(submission_id)
                self.house_resume.append(submission_id)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.pool.shutdown(wait=False, cancel_futures=True)
        for pool in (self.house_pool, self.propose_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def wake_house(self) -> None:
        """Safe from the threadpool that runs sync routes: idle house slots look again now. An
        admin resume also ends a failure backoff (an explicit "try again")."""
        self.loop.call_soon_threadsafe(self._reset_house_backoff)
        self.loop.call_soon_threadsafe(self.house_wake.set)

    def _reset_house_backoff(self) -> None:
        self.house_failures = 0
        self.house_backoff_until = None
        self._house_backoff_deadline = 0.0
        if self.propose_failures:  # an explicit "try again" asks the strategist again too
            self.propose_failures = 0
            self.propose_retry_at = None
            self._next_propose = 0.0

    def _house_failed(self) -> None:
        """Count a failed house run and push the lane's next start out."""
        self.house_failures += 1
        delay = min(HOUSE_FAIL_BACKOFF_S * 2 ** (self.house_failures - 1), HOUSE_FAIL_BACKOFF_MAX_S)
        self._house_backoff_deadline = self.loop.time() + delay
        self.house_backoff_until = self.store.now() + timedelta(seconds=delay)
        log.warning("house run failed (%d in a row); next in %.0f s", self.house_failures, delay)
        if self.house_failures == HOUSE_FAIL_NOTICE:
            self.store.add_event(
                f"house runs paused: runner failing; retrying in {delay:.0f}s", None
            )

    async def _refill(self) -> None:
        """Ask the strategist for a batch when fewer proposals wait than the lane has slots,
        at most once per house_propose_interval_s while it succeeds, and with the lane's
        doubling backoff while it fails. Never anything but wait on a failure."""
        async with self.propose_lock:  # one call at a time, whichever slot asks
            if self.settings.house_since is None:
                self.house_waiting = FACTORY_OFF
                return
            with logfire.suppress_instrumentation():
                unused = self.store.unused_proposals()
            if unused >= self.house_slots:
                self.house_waiting = None
                return
            if self.loop.time() < self._next_propose:
                return
            # Off the event loop: an unreachable market can take the fetch's whole timeout.
            bundle = (
                await asyncio.to_thread(self.bundles.get, fallback=False) if self.bundles else None
            )
            if bundle is None or not bundle.members:
                self.house_waiting = NO_UNIVERSE if bundle else WAITING_FOR_BUNDLE
                return
            try:
                await self.loop.run_in_executor(self.propose_pool, self._propose, bundle)
            except Exception as exc:  # noqa: BLE001 - any failure means wait
                self._propose_failed(exc)
                return
            self.propose_failures = 0
            self.propose_retry_at = None
            self.last_propose_error = None
            self._next_propose = self.loop.time() + self.settings.house_propose_interval_s
            self.house_waiting = None

    def _propose(self, bundle: Bundle) -> None:
        """brief() then propose() in this one worker thread; the proposals that pass the
        lane's checks are queued. Raises on any failure, and NoProposals when none pass."""
        strategist = self.strategist or import_strategist()
        exclude, existing, generation = self.store.factory_inputs()
        self.last_propose_at = self.store.now()
        with logfire.span("house propose", **{"bazaar.generation": generation}) as span:
            brief = strategist.brief(
                bundle_id=bundle.bundle_id,
                since=self.settings.house_since,
                exclude_names=exclude,
                data_version=self.settings.data_bundle,
            )
            names = list(brief.names)  # the brief is otherwise opaque here
            # The window's length, from the bundle route's sessions; left out (the strategist's
            # default) when the route lists none.
            window = {"trading_days": bundle.days} if bundle.days > 0 else {}
            proposals = strategist.propose(
                brief=brief,
                k=PROPOSE_K,
                generation=generation,
                instructions_by_name=self.store.instructions_for(names),
                existing_instructions=existing,
                universe=universe_of(bundle),
                benchmark=bundle.benchmark,
                **window,
            )
            allowed = {symbol for symbol, _, _ in bundle.members} | {bundle.benchmark or ""}
            outcome = self.store.add_proposals(
                proposals, generation=generation, allowed=allowed - {""}, parents=names
            )
            parents = sorted({p for x in proposals for p in (x.parents or ()) if p in names})
            span.set_attribute("bazaar.parents", parents)
            span.set_attribute("bazaar.proposals_added", outcome["added"])
        # Counts only: a proposal's text never reaches a log line.
        log.info("house proposals for generation %d: %s", generation, dict(sorted(outcome.items())))
        if not outcome["added"]:
            raise NoProposals

    def _propose_failed(self, exc: Exception) -> None:
        self.propose_failures += 1
        delay = min(
            HOUSE_FAIL_BACKOFF_S * 2 ** (self.propose_failures - 1), HOUSE_FAIL_BACKOFF_MAX_S
        )
        self._next_propose = self.loop.time() + delay
        self.propose_retry_at = self.store.now() + timedelta(seconds=delay)
        self.last_propose_error = type(exc).__name__
        self.house_waiting = WAITING_FOR_STRATEGIST
        # The class only: a strategist error's text could quote a response.
        log.warning(
            "house strategist failed (%s, %d in a row); next try in %.0f s",
            type(exc).__name__,
            self.propose_failures,
            delay,
        )

    async def _next_house(self) -> str | None:
        """The next house run to start: an interrupted one first, else the next proposal (the
        strategist is asked for more when they run low); None while paused, capped or waiting."""
        with logfire.suppress_instrumentation():
            if self.store.house_state()["paused"]:
                return None
        if self.house_resume:
            return self.house_resume.pop(0)
        await self._refill()
        with logfire.suppress_instrumentation():
            created = self.store.create_house(self.settings.house_max_per_hour)
        return created[0] if created else None

    async def _house_wait(self, seconds: float) -> None:
        """Sleep up to seconds; a resume (wake_house) ends it early."""
        self.house_wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.house_wake.wait(), seconds)

    async def _house_slot(self) -> None:
        """One house slot: run house strategies back to back while they succeed; idle while
        paused, capped or off, or while the lane backs off after failures."""
        while True:
            wait = self._house_backoff_deadline - self.loop.time()
            if wait > 0:
                await self._house_wait(wait)
                continue
            try:
                submission_id = await self._next_house()
            except Exception:  # a store error must not end the slot
                log.exception("house lane could not start a run")
                submission_id = None
            if submission_id is None:
                await self._house_wait(HOUSE_IDLE_S)
                continue
            try:
                await self._run(submission_id, self.house_pool)
            except Exception:
                log.exception("worker error on house submission %s", submission_id)
            with logfire.suppress_instrumentation():
                row = self.store.get(submission_id)
            if row is not None and row["status"] == "scored":
                self._reset_house_backoff()
            else:
                self._house_failed()

    def enqueue(self, submission_id: str) -> None:
        """Safe from the threadpool that runs sync routes."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, submission_id)

    async def _consume(self) -> None:
        while True:
            submission_id = await self.queue.get()
            try:
                await self._run(submission_id, self.pool)
            except Exception:
                log.exception("worker error on submission %s", submission_id)
            finally:
                self.queue.task_done()

    def _carrier(self, submission_id: str) -> dict[str, str]:
        with logfire.suppress_instrumentation():
            submission = self.store.get(submission_id)
        return carrier_of(submission) if submission else {}

    async def _run(self, submission_id: str, pool: ThreadPoolExecutor) -> None:
        with logfire.suppress_instrumentation():
            submission = self.store.get(submission_id)
        if submission is None or submission["status"] != "queued":
            return
        # Everything for this job, including its store writes, joins the submission's trace.
        with logfire.propagate.attach_context(carrier_of(submission)):
            await self._run_submission(submission, pool)

    async def _run_submission(self, submission: dict[str, Any], pool: ThreadPoolExecutor) -> None:
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
            # in_thread attaches the trace itself, so no context copy is needed (as to_thread does).
            run_dir = await self.loop.run_in_executor(pool, in_thread)
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
        self.store.finish(
            submission["id"], run_dir=None, error=error, error_class=type(exc).__name__
        )
        if submission.get("lane") == "house" and self.house_failures:
            return  # backing off: the lane's one notice stands in for each failure
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
