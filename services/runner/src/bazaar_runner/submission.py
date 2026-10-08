"""One web submission, run end to end: a single agent launch scored onto the leaderboard (C2).

run_submission is synchronous and self-contained so the web worker can call up to three at once
from threads: each call has its own event loop, HTTP clients and market experiment. The only
shared state is the one-time Logfire configuration.

Progress: after each of the 10 session closes the run calls on_progress(day, value) if the
callback accepts two positional arguments (signature.bind(day, value) succeeds, so (day, value),
(day, value=None) and *args all count), and on_progress(day) otherwise. value is the Decimal
portfolio value marked at that close (PortfolioSnapshot.portfolio_value, value-v1). The shape is
decided once per run, so existing one-argument callbacks keep working. A callback that raises is
logged and never stops the run.
"""

import asyncio
import inspect
import logging
import os
import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from bazaar_agent.strategy_evaluation import strategy_evaluation_session
from bazaar_agent.trading import DecisionBudget, RuntimeConfig
from bazaar_protocol.telemetry import redact

from bazaar_runner.demo import DEMO_SYMBOLS, Launch, demo_spec
from bazaar_runner.http_market import HttpMarketPort
from bazaar_runner.market import MarketError
from bazaar_runner.record import record_run
from bazaar_runner.telemetry import configure_telemetry

logger = logging.getLogger(__name__)

DATA_VERSION = "demo-bundle-v1"
EXECUTION_RULE_VERSION = "exec-v1"
STARTING_CASH = Decimal(10000)
MARKET_TIMEOUT_SECONDS = 30.0

# PM 14:01Z and 14:08Z: submissions get 8 model requests and 48,000 tokens (smoke #2 hit the
# 16k token limit at 3-4 requests). Tool calls and timeout track the harness defaults, and every
# other launch keeps DecisionBudget() as it is.
SUBMISSION_BUDGET = DecisionBudget().model_copy(
    update={"model_requests": 8, "total_tokens": 48_000}
)
# PM 14:02Z, security: attendee text is untrusted and runs beside the Gateway key and the runner
# token. Code mode is pinned off here and never taken from env, arguments or the submission.
# Tracing with message content is on (Anthony 10-08): secrets never enter the agent's context.
SUBMISSION_RUNTIME = RuntimeConfig(code_mode=False, instrument=True)


class SubmissionFailed(Exception):
    """The submission did not produce a scored run. The message is safe to show the submitter."""


def submission_experiment_id(submission_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"bazaar:sub-{submission_id}")


def _transport_for(market_url: str) -> httpx.AsyncBaseTransport | None:
    """The real network in production; tests replace this to serve an in-memory market."""
    return None


def _model_factory(model: str | None) -> Any:
    """None lets run_decision build the operator's default model from the environment."""
    if model is None:
        return None
    from bazaar_agent.trading import env_model_factory

    return env_model_factory(model)


ProgressReporter = Callable[[int, Decimal], None]


def _progress_reporter(on_progress: Callable[..., None] | None) -> ProgressReporter | None:
    """Decide once whether the callback takes (day, value) or only (day) (PM 15:40Z rule)."""
    if on_progress is None:
        return None
    try:
        inspect.signature(on_progress).bind(1, Decimal(0))
    except (TypeError, ValueError):  # cannot take two positional arguments, or not introspectable
        return lambda day, value: on_progress(day)
    return on_progress


class _ProgressPort:
    """The market port, reporting each session close: portfolio() is read only at MARK events."""

    def __init__(self, port: HttpMarketPort, report: ProgressReporter | None) -> None:
        self._port = port
        self._report = report
        self._day = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    async def portfolio(self, ctx):
        snapshot = await self._port.portfolio(ctx)
        self._day += 1
        if self._report is not None:
            try:
                self._report(self._day, snapshot.portfolio_value)
            except Exception:
                logger.exception("on_progress(%d) failed; the run continues", self._day)
        return snapshot


def run_submission(
    *,
    submission_id: str,
    name: str,
    instructions: str,
    market_url: str,
    runner_token: str,
    runs_dir: Path,
    model: str | None = None,
    on_progress: Callable[..., None] | None = None,
    handle: str | None = None,
) -> Path:
    """Run one agent over the demo fortnight and return its scored run directory.

    The run directory appears in runs_dir all at once (record.json and evaluation.json) or not
    at all. Any failure raises SubmissionFailed with a readable message and leaves nothing behind.
    """
    # Configures Logfire only if nothing in this process has (the web app usually has).
    configure_telemetry()
    staging = runs_dir / f".tmp-{uuid4().hex}"
    try:
        run_dir = asyncio.run(
            _run(
                submission_id=submission_id,
                name=name,
                instructions=instructions,
                market_url=market_url,
                runner_token=runner_token,
                staging=staging,
                runs_dir=runs_dir,
                model=model,
                on_progress=on_progress,
                handle=handle,
            )
        )
    except SubmissionFailed as exc:
        raise SubmissionFailed(redact(str(exc), runner_token)) from None
    except MarketError as exc:
        message = f"the market refused the run: {exc.detail.message}"
        raise SubmissionFailed(redact(message, runner_token)) from None
    except Exception as exc:  # noqa: BLE001 - only the type: details could quote configuration
        raise SubmissionFailed(f"the run could not complete ({type(exc).__name__})") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return run_dir


async def _run(
    *,
    submission_id: str,
    name: str,
    instructions: str,
    market_url: str,
    runner_token: str,
    staging: Path,
    runs_dir: Path,
    model: str | None,
    on_progress: Callable[..., None] | None,
    handle: str | None,
) -> Path:
    # Imported here: bazaar_agent and bazaar_evaluation are optional for the runner package.
    from bazaar_evaluation import evaluate_and_emit

    from bazaar_runner.agent import make_agent_decider
    from bazaar_runner.agent_step import AgentStep

    experiment_id = submission_experiment_id(submission_id)
    approval_id = uuid4()
    launch = Launch(f"submission:{name}", experiment_id, approval_id)
    spec = demo_spec(
        launch,
        data_version=DATA_VERSION,
        execution_rule_version=EXECUTION_RULE_VERSION,
        starting_cash=STARTING_CASH,
    )
    transport = _transport_for(market_url)
    async with (
        strategy_evaluation_session(),
        httpx.AsyncClient(
            base_url=market_url, timeout=MARKET_TIMEOUT_SECONDS, transport=transport
        ) as client,
    ):
        port = HttpMarketPort(client, experiment_id, approval_id, runner_token)
        await port.grant(approval_id)
        step = AgentStep(
            # The submitter's text is untrusted strategy input; run_decision labels it as such.
            make_agent_decider(
                instructions,
                _model_factory(model),
                budget=SUBMISSION_BUDGET,
                runtime=SUBMISSION_RUNTIME,
                quote_symbols=DEMO_SYMBOLS,
            ),
            market_url=market_url,
            symbols=DEMO_SYMBOLS,
            transport=transport,
        )
        record, evaluation = await record_run(
            spec,
            _ProgressPort(port, _progress_reporter(on_progress)),
            step,
            policy_ref=launch.policy_ref,
            runs_dir=staging,
            evaluate=evaluate_and_emit,
            submission_id=submission_id,
            strategy_name=name,
            handle=handle,
        )
    if record.status != "completed":
        raise SubmissionFailed(f"the run failed: {record.failure}")
    # The board shows a submission as scored from evaluation.json: no score, no published run.
    if evaluation is None:
        raise SubmissionFailed("the run could not be scored")
    run_dir = runs_dir / str(spec.run_id)
    os.replace(staging / str(spec.run_id), run_dir)
    return run_dir
