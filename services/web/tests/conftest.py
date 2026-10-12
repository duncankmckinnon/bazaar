"""Offline fixtures: synthetic run directories shaped like RunRecord v1 / RunEvaluation."""

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import bazaar_web.telemetry
import bazaar_web.worker
import httpx
import pytest
from bazaar_web.settings import Settings

START = "2026-02-02T14:30:00Z"
END = "2026-02-13T21:00:00Z"
NAMESPACE = uuid5(NAMESPACE_URL, "bazaar-web-tests")


def write_run(
    runs_dir: Path,
    run_id: str,
    *,
    policy_ref: str,
    period_return: str | None,
    status: str = "completed",
    failure: str | None = None,
    fills: int = 1,
    marks: tuple[str, ...] = (),
    trace_id: str | None = None,
) -> Path:
    account_id = str(uuid5(NAMESPACE, f"{run_id}-account")) if status == "completed" else None
    manifest = {
        "experiment_id": str(uuid5(NAMESPACE, f"{run_id}-experiment")),
        "agent_id": str(uuid5(NAMESPACE, f"{run_id}-agent")),
        "account_id": account_id,
        "strategy_version_id": str(uuid5(NAMESPACE, f"{run_id}-strategy")),
        "approval_id": str(uuid5(NAMESPACE, f"{run_id}-approval")),
        "data_version": "demo-bundle-v1",
        "execution_rule_version": "exec-v1",
        "schedule_digest": "sched-v1:0123456789abcdef",
        "period_start": START,
        "period_end": END,
        "starting_cash": "10000",
        "policy_ref": policy_ref,
    }
    record = {
        "record_version": "runrecord-v1",
        "manifest": manifest,
        "status": status,
        "failure": failure,
        "failure_code": "approval_denied" if status == "failed" else None,
        "trace_id": trace_id,
        "orders": [
            {"event_sequence": i, "order_index": 0, "result": {"status": "filled"}}
            for i in range(fills)
        ],
        "marks": [
            {"event_sequence": 100 + i, "snapshot": {"portfolio_value": value}}
            for i, value in enumerate(marks)
        ],
        "final_account": None,
    }
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "record.json").write_text(json.dumps(record))
    if status == "completed":
        evaluation = {
            "experiment_id": manifest["experiment_id"],
            "account_id": account_id,
            "evaluator_version": "evals-v1",
            "run_status": "completed",
            "trade_scores": [],
            "period": {"status": "scored", "period_return": period_return, "reconciled": True},
        }
        (run_dir / "evaluation.json").write_text(json.dumps(evaluation))
    return run_dir


def seed_runs(runs_dir: Path) -> None:
    write_run(runs_dir, "seed-bh", policy_ref="baseline-buy-and-hold", period_return="0.0310")
    write_run(runs_dir, "seed-cash", policy_ref="baseline-cash-only", period_return="0", fills=0)
    write_run(
        runs_dir,
        "seed-agent",
        policy_ref="scripted-momentum-v1",
        period_return="0.006011",
        fills=3,
        marks=("10000", "10025.50", "10060.11"),
        trace_id="0af7651916cd43dd8448eb211c80319c",
    )
    write_run(
        runs_dir,
        "seed-refused",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        status="failed",
        failure="refused before any account was opened",
    )


class FakeRunner:
    """Stands in for bazaar_runner.submission.run_submission (C2)."""

    def __init__(
        self,
        returns: dict[str, str] | None = None,
        fail: set[str] = frozenset(),
        values: dict[str, str] | None = None,
    ):
        self.returns = returns or {}
        self.fail = fail
        # name -> marked value reported with each day, like the newer runner; else day only.
        self.values = values or {}
        self.gate = threading.Event()
        self.gate.set()
        self.calls: list[dict] = []

    def __call__(self, *, submission_id, name, instructions, market_url, runner_token, runs_dir,
                 model=None, on_progress=None) -> Path:  # fmt: skip
        # Like the real run_submission, which calls asyncio.run: this raises if the worker ever
        # calls it on the event loop instead of in a thread.
        asyncio.run(asyncio.sleep(0))
        self.calls.append(
            {"submission_id": submission_id, "name": name, "runner_token": runner_token}
        )
        for day in range(1, 11):
            if on_progress and name in self.values:
                on_progress(day, Decimal(self.values[name]))
            elif on_progress:
                on_progress(day)
        if not self.gate.wait(timeout=10):
            raise TimeoutError("test gate never opened")
        if name in self.fail:
            raise RuntimeError(
                f"market said no: token=super-secret-token key={os.environ.get('PYDANTIC_AI_GATEWAY_API_KEY')}"
            )
        return write_run(
            runs_dir,
            f"sub-{submission_id}",
            policy_ref=f"submission-{name}",
            period_return=self.returns.get(name, "0.0120"),
            marks=("10000", "10120"),
        )


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def release(runner) -> None:
    """Open every gate a fake runner (or a wrapper around one, via .fake) is holding."""
    while runner is not None:
        gate = getattr(runner, "gate", None)
        if isinstance(gate, threading.Event):
            gate.set()
        if callable(getattr(runner, "release_all", None)):
            runner.release_all()
        runner = getattr(runner, "fake", None)


@pytest.fixture(autouse=True)
def drain_background_work(monkeypatch):
    """Join every app's run threads before the next test starts.

    Worker.stop() shuts its pools down without waiting (right in production: a real run takes
    minutes and recovery reruns it after a restart). In tests that let a fake run, still
    reporting progress, write rows and sqlite spans into the NEXT test's capfire. So each
    Worker made during a test is tracked, its runner released, and its run and house pools
    joined here.
    """
    workers = []
    real_init = bazaar_web.worker.Worker.__init__

    def tracked(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        workers.append(self)

    monkeypatch.setattr(bazaar_web.worker.Worker, "__init__", tracked)
    yield
    for worker in workers:
        release(worker.run_submission)
        if hasattr(worker, "dataset"):
            worker.dataset.stop()
        pools = ("pool", "house_pool", "propose_pool", "dataset_pool")
        for pool in (getattr(worker, name, None) for name in pools):
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)


REAL_CONFIGURE = bazaar_web.telemetry.configure


@pytest.fixture(autouse=True)
def no_global_telemetry_setup(monkeypatch):
    """The lifespan configures Logfire for real; tests keep capfire's (or no) configuration."""
    monkeypatch.setattr("bazaar_web.telemetry.configure", lambda: None)
    monkeypatch.setenv("LOGFIRE_IGNORE_NO_CONFIG", "1")


@pytest.fixture(autouse=True)
def no_dataset_key(monkeypatch):
    """No test pushes to the real Logfire dataset: a key in the shell never reaches a test."""
    monkeypatch.delenv("LOGFIRE_API_WRITE_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_market_bundle(monkeypatch):
    """Apps built without a bundle source get a market that has no bundle (404), offline."""
    from bazaar_web.bundle import BundleSource

    def offline(market_url, bundle_id, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(lambda request: httpx.Response(404)))
        return BundleSource(market_url, bundle_id, **kwargs)

    monkeypatch.setattr("bazaar_web.app.BundleSource", offline)


@pytest.fixture
def real_configure():
    return REAL_CONFIGURE.__wrapped__  # undecorated: no once-per-process cache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        runner_token="super-secret-token",
        runs_dir=tmp_path / "runs",
        web_db=tmp_path / "data" / "web.sqlite3",
        admin_token="admin-secret",
        # A state change shows on the next poll once its rebuild ends; tests wait for that
        # rebuild rather than racing it (live waits 1 s, then serves the previous board).
        board_fresh_wait_ms=60_000,
    )


@pytest.fixture
def seeded(settings) -> Settings:
    seed_runs(settings.runs_dir)
    return settings


@dataclass(frozen=True)
class FakeProposal:
    name: str
    instructions: str
    parents: list[str] = field(default_factory=list)
    idea: str = "an idea"


@dataclass(frozen=True)
class FakeBrief:
    """Opaque to the lane, which reads only .names (like PerformanceBrief.names)."""

    names: list[str]


OMITTED = object()  # an argument the caller left out


class FakeStrategist:
    """bazaar_agent.strategist's brief() and propose(), with the agreed F1/F2 signatures.

    By default each propose() returns k fresh proposals named g<generation>-s<n> over KO and
    MSFT, with the brief's first name as parent; batches (lists of proposals) replace that, one
    per call. fail, when set, is raised by brief().
    """

    def __init__(self, *, names=("parent-a", "parent-b"), batches=None, fail=None):
        self.names = list(names)
        self.batches = list(batches or [])
        self.fail = fail
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.proposed: list[str] = []  # every name returned, in order
        self.count = 0

    def brief(
        self, *, bundle_id, since, environment="production", top=10, bottom=5,
        exclude_names=frozenset(), data_version=None, house_names=OMITTED,
    ):  # fmt: skip
        with self.lock:
            self.calls.append(("brief", {
                "bundle_id": bundle_id, "since": since, "environment": environment,
                "exclude_names": exclude_names, "data_version": data_version,
                **({} if house_names is OMITTED else {"house_names": house_names}),
            }))  # fmt: skip
        if self.fail is not None:
            raise self.fail
        return FakeBrief(list(self.names))

    def propose(
        self, *, brief, k, generation, instructions_by_name, existing_instructions, universe,
        benchmark, trading_days=OMITTED,
    ):  # fmt: skip
        with self.lock:
            self.calls.append(("propose", {
                "brief": brief, "k": k, "generation": generation,
                "instructions_by_name": instructions_by_name,
                "existing_instructions": existing_instructions, "universe": universe,
                "benchmark": benchmark,
                **({} if trading_days is OMITTED else {"trading_days": trading_days}),
            }))  # fmt: skip
            if self.batches:
                batch = list(self.batches.pop(0))
            else:
                batch = []
                for _ in range(k):
                    self.count += 1
                    batch.append(FakeProposal(
                        f"g{generation}-s{self.count:03d}",
                        f"Buy KO on day 1, then put {self.count} percent into MSFT on day 2.",
                        parents=brief.names[:1],
                    ))  # fmt: skip
            self.proposed += [p.name for p in batch]
        return batch


@pytest.fixture
def helpers():
    class H:
        FakeStrategist = FakeStrategist
        FakeProposal = FakeProposal
        FakeRunner = FakeRunner
        Clock = Clock
        wait_for = staticmethod(wait_for)
        write_run = staticmethod(write_run)
        Decimal = Decimal

    return H
