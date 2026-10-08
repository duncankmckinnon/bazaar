"""Offline fixtures: synthetic run directories shaped like RunRecord v1 / RunEvaluation."""

import asyncio
import json
import os
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import bazaar_web.telemetry
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


REAL_CONFIGURE = bazaar_web.telemetry.configure


@pytest.fixture(autouse=True)
def no_global_telemetry_setup(monkeypatch):
    """The lifespan configures Logfire for real; tests keep capfire's (or no) configuration."""
    monkeypatch.setattr("bazaar_web.telemetry.configure", lambda: None)
    monkeypatch.setenv("LOGFIRE_IGNORE_NO_CONFIG", "1")


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
    )


@pytest.fixture
def seeded(settings) -> Settings:
    seed_runs(settings.runs_dir)
    return settings


@pytest.fixture
def helpers():
    class H:
        FakeRunner = FakeRunner
        Clock = Clock
        wait_for = staticmethod(wait_for)
        write_run = staticmethod(write_run)
        Decimal = Decimal

    return H
