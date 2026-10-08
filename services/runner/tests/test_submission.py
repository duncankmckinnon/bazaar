import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import httpx
import pytest
from bazaar_agent import trading
from bazaar_protocol import ApiError, ErrorCode, ErrorDetail
from bazaar_replay.leaderboard import Run, load_board, load_run
from bazaar_runner import submission
from bazaar_runner.agent import fixture_model_factory
from bazaar_runner.http_market import APPROVAL_HEADER, RUNNER_TOKEN_HEADER
from bazaar_runner.market import MarketError
from bazaar_runner.record import RunRecord
from bazaar_runner.submission import (
    SUBMISSION_BUDGET,
    SUBMISSION_RUNTIME,
    SubmissionFailed,
    run_submission,
    submission_experiment_id,
)

from .market_fakes import TOKEN, InMemoryMarket, delegating_transport

INSTRUCTIONS = "Read the news once, buy ten AAPL at the first open, then hold."


def api_error(status: int, code: ErrorCode, message: str) -> httpx.Response:
    body = ApiError(error=ErrorDetail(code=code, message=message))
    return httpx.Response(status, json=json.loads(body.model_dump_json()))


class GrantingMarket:
    """The in-memory market behind HTTP, with the market's C1 grants: an approval works only
    once the runner has bound it to an experiment with its token."""

    def __init__(self, *, refuse_grant: bool = False) -> None:
        self.fake = InMemoryMarket()
        self.grants: dict[str, str] = {}
        self.refuse_grant = refuse_grant
        self.paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            if request.url.path == "/control/grants":
                if request.headers.get(RUNNER_TOKEN_HEADER) != TOKEN:
                    return api_error(401, ErrorCode.UNAUTHORIZED, "runner token required")
                body = json.loads(request.content)
                approval, experiment = body["approval_id"], body["experiment_id"]
                if self.refuse_grant or self.grants.get(approval, experiment) != experiment:
                    # A careless market might echo what it received; the runner must redact it.
                    return api_error(409, ErrorCode.CONFLICT, f"bound elsewhere (token {TOKEN})")
                self.grants[approval] = experiment
                return httpx.Response(204)
            approval = request.headers.get(APPROVAL_HEADER, "")
            if approval not in self.grants:
                return api_error(403, ErrorCode.EXPERIMENT_NOT_APPROVED, "not approved")
            served = delegating_transport(self.fake, approval_id=UUID(approval))
            return await served.handle_async_request(request)

        return httpx.MockTransport(handle)


@pytest.fixture
def markets(monkeypatch):
    """One market per URL; run_submission reaches them through its transport hook."""
    by_url: dict[str, GrantingMarket] = {}
    transports: dict[str, httpx.MockTransport] = {}

    def transport_for(url: str) -> httpx.MockTransport:
        if url not in transports:
            transports[url] = by_url[url].transport()
        return transports[url]

    monkeypatch.setattr(submission, "_transport_for", transport_for)
    # The declared fixture agent (news once, buy 10 AAPL, then hold): one model per submission.
    monkeypatch.setattr(submission, "_model_factory", lambda model: fixture_model_factory())
    monkeypatch.setattr(submission, "configure_telemetry", lambda: None)
    return by_url


def submit(tmp_path, url: str, submission_id: str = "sub-1", name: str = "momo", **kwargs):
    days: list[int] = []
    run_dir = run_submission(
        submission_id=submission_id,
        name=name,
        instructions=INSTRUCTIONS,
        market_url=url,
        runner_token=TOKEN,
        runs_dir=tmp_path,
        on_progress=days.append,
        **kwargs,
    )
    return run_dir, days


def record_in(run_dir) -> RunRecord:
    return RunRecord.model_validate_json((run_dir / "record.json").read_text())


def test_a_submission_runs_scores_and_reaches_the_board(markets, tmp_path):
    markets["http://m1"] = GrantingMarket()
    run_dir, days = submit(tmp_path, "http://m1")

    assert days == list(range(1, 11))
    assert sorted(p.name for p in tmp_path.iterdir()) == [run_dir.name]
    assert (run_dir / "evaluation.json").exists()
    record = record_in(run_dir)
    assert record.status == "completed"
    assert record.submission_id == "sub-1"
    assert record.manifest.policy_ref == "submission:momo"
    assert record.manifest.experiment_id == submission_experiment_id("sub-1")
    assert record.manifest.data_version == "demo-bundle-v1"
    assert record.manifest.execution_rule_version == "exec-v1"
    assert record.manifest.starting_cash == 10000
    (order,) = record.orders
    assert order.result.status == "filled" and order.request.symbol == "AAPL"
    assert record.agent_usage is not None and record.agent_usage.model_requests >= 10

    loaded = load_run(run_dir)
    assert isinstance(loaded, Run) and loaded.evaluation is not None
    board = load_board(tmp_path)
    assert [e.run_id for e in board.ranked] == [run_dir.name] and board.invalid == ()


def test_the_experiment_id_is_the_agreed_uuid5():
    from uuid import NAMESPACE_URL, uuid5

    assert submission_experiment_id("abc") == uuid5(NAMESPACE_URL, "bazaar:sub-abc")


def test_a_refused_grant_raises_and_leaves_nothing(markets, tmp_path):
    markets["http://m1"] = GrantingMarket(refuse_grant=True)
    with pytest.raises(SubmissionFailed) as error:
        submit(tmp_path, "http://m1")
    assert "the market refused the run" in str(error.value)
    assert TOKEN not in str(error.value) and "[REDACTED]" in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_a_market_failure_mid_run_raises_and_leaves_nothing(markets, tmp_path):
    market = GrantingMarket()
    markets["http://m1"] = market
    cutoffs = []
    original = market.fake.set_cutoff

    async def fails_on_day_five(experiment_id, cutoff, *versions):
        cutoffs.append(cutoff)
        if len(cutoffs) == 10:
            raise MarketError(ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="disk full"))
        return await original(experiment_id, cutoff, *versions)

    market.fake.set_cutoff = fails_on_day_five
    with pytest.raises(SubmissionFailed) as error:
        submit(tmp_path, "http://m1")
    assert str(error.value).startswith("the run failed: ")
    assert "disk full" in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_a_run_that_cannot_be_scored_raises_and_leaves_nothing(markets, tmp_path, monkeypatch):
    import bazaar_evaluation

    markets["http://m1"] = GrantingMarket()

    def evals_down(record):
        raise RuntimeError("evaluator crashed")

    monkeypatch.setattr(bazaar_evaluation, "evaluate_and_emit", evals_down)
    with pytest.raises(SubmissionFailed, match="^the run could not be scored$"):
        submit(tmp_path, "http://m1")
    assert list(tmp_path.iterdir()) == []


def test_a_progress_callback_that_raises_does_not_stop_the_run(markets, tmp_path):
    markets["http://m1"] = GrantingMarket()

    def broken(day):
        raise RuntimeError("display went away")

    run_dir = run_submission(
        submission_id="sub-1",
        name="momo",
        instructions=INSTRUCTIONS,
        market_url="http://m1",
        runner_token=TOKEN,
        runs_dir=tmp_path,
        on_progress=broken,
    )
    assert record_in(run_dir).status == "completed"


def test_the_runner_token_never_reaches_the_files(markets, tmp_path):
    markets["http://m1"] = GrantingMarket()
    run_dir, _ = submit(tmp_path, "http://m1")
    for path in run_dir.rglob("*"):
        assert TOKEN not in path.read_text()


def test_submissions_get_their_budget_and_never_code_mode(markets, tmp_path, monkeypatch):
    markets["http://m1"] = GrantingMarket()
    seen = []
    real = trading.run_decision

    async def spy(**kwargs):
        seen.append((kwargs["budget"], kwargs["runtime"]))
        return await real(**kwargs)

    def no_code_mode(*args, **kwargs):
        raise AssertionError("CodeMode must never be built for a submission")

    monkeypatch.setattr(trading, "run_decision", spy)
    monkeypatch.setattr(trading, "CodeMode", no_code_mode)
    run_dir, _ = submit(tmp_path, "http://m1")

    assert record_in(run_dir).status == "completed" and len(seen) == 10
    assert {(b.model_requests, b.tool_calls, b.total_tokens, r.code_mode) for b, r in seen} == {
        (8, 20, 48_000, False)
    }
    assert all(b == SUBMISSION_BUDGET and r == SUBMISSION_RUNTIME for b, r in seen)
    # Only those two limits differ; the default every other launch uses is unchanged.
    default = trading.DecisionBudget()
    assert (default.model_requests, default.tool_calls, default.total_tokens) == (4, 20, 16_000)
    assert SUBMISSION_BUDGET.model_copy(update={"model_requests": 4, "total_tokens": 16_000}) == (
        default
    )


@pytest.mark.parametrize("online_evaluation", [False, True])
def test_three_concurrent_submissions_do_not_cross(
    markets, tmp_path, monkeypatch, capfire, online_evaluation
):
    if online_evaluation:
        import httpx2
        from bazaar_agent import strategy_evaluation

        async def grade(request):
            # Keep the last evaluation pending when the trading loop finishes.
            await asyncio.sleep(0.01)
            return httpx2.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"pass": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 120, "output_tokens": 1},
                },
            )

        provider = strategy_evaluation.TypeSafeProvider
        monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
        monkeypatch.delenv("BAZAAR_JUDGE_MODEL", raising=False)
        monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "test-gateway-key")
        monkeypatch.setenv("PYDANTIC_AI_GATEWAY_BASE_URL", "https://gateway.test/proxy")
        monkeypatch.setattr(
            strategy_evaluation,
            "TypeSafeProvider",
            lambda **kwargs: provider(
                **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(grade))
            ),
        )
    ids = ["alpha", "bravo", "charlie"]
    for sid in ids:
        markets[f"http://{sid}"] = GrantingMarket()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            sid: pool.submit(submit, tmp_path, f"http://{sid}", sid, f"name-{sid}") for sid in ids
        }
        results = {sid: future.result() for sid, future in futures.items()}

    dirs = {run_dir for run_dir, _ in results.values()}
    assert len(dirs) == 3 and sorted(p.name for p in tmp_path.iterdir()) == sorted(
        d.name for d in dirs
    )
    for sid, (run_dir, days) in results.items():
        record = record_in(run_dir)
        assert record.submission_id == sid and record.status == "completed"
        assert record.manifest.experiment_id == submission_experiment_id(sid)
        assert record.manifest.policy_ref == f"submission:name-{sid}"
        assert days == list(range(1, 11))
        # Each market only ever saw its own experiment.
        own = str(submission_experiment_id(sid))
        assert all(
            own in p for p in markets[f"http://{sid}"].paths if p.startswith("/experiments/")
        )
    assert len(load_board(tmp_path).ranked) == 3
    events = [
        event
        for event in capfire.log_exporter.exported_logs_as_dicts()
        if event["attributes"].get("gen_ai.evaluation.name") == "strategy_adherence"
    ]
    assert len(events) == (30 if online_evaluation else 0)
    if online_evaluation:
        for sid in ids:
            own_events = [
                event for event in events if event["attributes"]["bazaar.submission_id"] == sid
            ]
            assert len(own_events) == 10
            assert all(
                event["attributes"]["bazaar.strategy_name"] == f"name-{sid}" for event in own_events
            )
    assert all(
        "strategy_adherence" not in (directory / "record.json").read_text() for directory in dirs
    )


def run_with(tmp_path, on_progress):
    return run_submission(
        submission_id="sub-marks",
        name="marks",
        instructions=INSTRUCTIONS,
        market_url="http://m1",
        runner_token=TOKEN,
        runs_dir=tmp_path,
        on_progress=on_progress,
    )


def test_a_one_argument_progress_callback_still_gets_the_day(markets, tmp_path):
    markets["http://m1"] = GrantingMarket()
    days = []

    def day_only(day):
        days.append(day)

    run_with(tmp_path, day_only)
    assert days == list(range(1, 11))


def test_a_two_argument_progress_callback_gets_each_closes_marked_value(markets, tmp_path):
    from decimal import Decimal

    markets["http://m1"] = GrantingMarket()
    seen = []

    def day_and_value(day, value):
        seen.append((day, value))

    run_dir = run_with(tmp_path, day_and_value)
    marks = record_in(run_dir).marks
    assert [day for day, _ in seen] == list(range(1, 11))
    assert all(isinstance(value, Decimal) for _, value in seen)
    assert [value for _, value in seen] == [m.snapshot.portfolio_value for m in marks]


def test_a_var_args_progress_callback_gets_both(markets, tmp_path):
    markets["http://m1"] = GrantingMarket()
    seen = []
    run_dir = run_with(tmp_path, lambda *args: seen.append(args))
    marks = record_in(run_dir).marks
    assert seen == [(i + 1, m.snapshot.portfolio_value) for i, m in enumerate(marks)]


def test_a_progress_callback_with_an_optional_value_gets_it(markets, tmp_path):
    markets["http://m1"] = GrantingMarket()
    seen = []

    def day_and_optional_value(day, value=None):
        seen.append((day, value))

    run_dir = run_with(tmp_path, day_and_optional_value)
    marks = record_in(run_dir).marks
    assert seen == [(i + 1, m.snapshot.portfolio_value) for i, m in enumerate(marks)]


def test_keyword_only_value_is_not_passed_positionally():
    from bazaar_runner.submission import _progress_reporter

    seen = []

    def keyword_value(day, *, value=None):
        seen.append((day, value))

    _progress_reporter(keyword_value)(3, 7)
    assert seen == [(3, None)]
