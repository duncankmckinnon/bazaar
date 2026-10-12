"""Exercise real online evaluation and OTel emission against a local Gateway Jev route."""

import asyncio
import json

import httpx
import httpx2
import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_evals.online import wait_for_evaluations

from .test_research import account, news, order, order_request, page, portfolio
from .test_trading import (
    DecisionBudget,
    RuntimeConfig,
    code_call,
    code_runtime,
    invoke,
    news_code,
    output,
    query,
    script,
)


def evaluations(capfire):
    return [
        entry
        for entry in capfire.log_exporter.exported_logs_as_dicts()
        if entry["attributes"].get("gen_ai.evaluation.target") == "trading.decision"
    ]


GATEWAY = "https://gateway.test/proxy"


def verdict(probability):
    return {
        "model": "jev-1.13.0",  # Jev resolves the jev-latest alias.
        "answers": {"pass": {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 120, "output_tokens": 1},
    }


def route_judge(monkeypatch, handler):
    """Serve the Gateway's Jev route locally; everything else is the production judge (its
    client is still built, instrumented and closed by judge_http_client)."""
    from bazaar_agent import strategy_evaluation

    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.delenv("BAZAAR_JUDGE_MODEL", raising=False)
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_BASE_URL", GATEWAY)
    monkeypatch.setattr(
        strategy_evaluation,
        "_new_http_client",
        lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


@pytest.fixture
def judge(monkeypatch):
    # Production must pass only decision evidence to the judge, never runtime handles.
    prompts = []

    def grade(request):
        assert str(request.url) == f"{GATEWAY}/jev-duncan/v1/systemone"
        assert request.headers["authorization"] == "Bearer test-gateway-key"
        body = json.loads(request.content)
        assert body["model"] == "jev-latest" and set(body["questions"]) == {"pass"}
        prompts.append(body["state"])
        return httpx2.Response(200, json=verdict(0.9))

    route_judge(monkeypatch, grade)
    return prompts


@pytest.mark.parametrize("instrument", [False, True])
async def test_judge_receives_strategy_snapshots_research_and_hold(judge, capfire, instrument):
    model, _ = script(
        [ToolCallPart("news", query())],
        lambda info: [output(info)],
    )
    result, requests = await invoke(
        model,
        payload=page([news()]),
        instructions="Read the news, then hold.",
        overrides={"runtime": RuntimeConfig(instrument=instrument)},
    )
    await wait_for_evaluations()

    assert result.error is None and result.decision.action == "hold"
    assert len(requests) == 1 and len(judge) == 1
    prompt = judge[0]
    assert "Read the news, then hold." in prompt
    assert news()["headline"] in prompt
    assert "portfolio_value" in prompt and "cash" in prompt
    assert "hold" in prompt
    assert "AsyncClient" not in prompt and "model_factory" not in prompt
    events = evaluations(capfire)
    by_name = {event["attributes"]["gen_ai.evaluation.name"]: event for event in events}
    score = by_name["strategy_adherence"]["attributes"]
    assert score["gen_ai.evaluation.score.value"] == 1.0
    assert "gen_ai.evaluation.explanation" not in score  # Jev returns no rationale.
    assert (
        by_name["strategy_adherence_pass"]["attributes"]["gen_ai.evaluation.score.label"] == "pass"
    )
    probability = by_name["strategy_adherence_probability"]["attributes"]
    assert probability["gen_ai.evaluation.score.value"] == pytest.approx(0.9)
    assert "strategy_adherence_confidence" not in by_name
    wrapper = next(
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "trading.decision.evaluated"
    )
    assert all(event["span_id"] == wrapper["context"]["span_id"] for event in events)
    # Evaluation adds no fields to the persisted decision contract.
    assert set(result.model_dump()) == {
        "decision",
        "error",
        "usage",
        "order_request",
        "order_result",
    }


async def test_failed_judge_does_not_change_a_decision(monkeypatch, capfire):
    route_judge(monkeypatch, lambda request: httpx2.Response(503, text="judge unavailable"))
    result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    await wait_for_evaluations()
    assert result.error is None and result.decision.action == "hold"
    [event] = evaluations(capfire)
    assert event["attributes"]["error.type"] == "ModelHTTPError"
    assert "gen_ai.evaluation.score.value" not in event["attributes"]


@pytest.mark.parametrize("judge_model", ["jev-latest", "jev/duncan:jev-latest", "jev-duncan:"])
async def test_judge_model_must_name_a_gateway_route_and_model(monkeypatch, capfire, judge_model):
    route_judge(monkeypatch, lambda request: pytest.fail("A malformed judge model was called"))
    monkeypatch.setenv("BAZAAR_JUDGE_MODEL", judge_model)
    result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    await wait_for_evaluations()
    assert result.error is None
    [event] = evaluations(capfire)
    assert event["attributes"]["error.type"] == "UserError"


async def test_missing_gateway_key_is_an_evaluation_error(monkeypatch, capfire):
    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.delenv("PYDANTIC_AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("PAIG_API_KEY", raising=False)
    result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    await wait_for_evaluations()
    assert result.error is None
    [event] = evaluations(capfire)
    assert event["attributes"]["error.type"] == "UserError"


async def test_violation_reports_jev_adherence_probability(monkeypatch, capfire):
    route_judge(monkeypatch, lambda request: httpx2.Response(200, json=verdict(0.2)))
    await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    await wait_for_evaluations()
    by_name = {e["attributes"]["gen_ai.evaluation.name"]: e for e in evaluations(capfire)}
    assert by_name["strategy_adherence"]["attributes"]["gen_ai.evaluation.score.value"] == 0.0
    assert (
        by_name["strategy_adherence_pass"]["attributes"]["gen_ai.evaluation.score.label"] == "fail"
    )
    assert by_name["strategy_adherence_probability"]["attributes"][
        "gen_ai.evaluation.score.value"
    ] == pytest.approx(0.2)
    assert "strategy_adherence_confidence" not in by_name


async def test_each_concurrent_decision_is_evaluated(judge, capfire):
    results = await asyncio.gather(
        *[
            invoke(
                TestModel(call_tools=[], custom_output_args={"action": "hold"}),
                instructions=f"Hold strategy [{i}]",
            )
            for i in range(12)
        ]
    )
    await wait_for_evaluations()
    assert all(result.error is None for result, _ in results)
    assert len(judge) == 12
    for i in range(12):
        assert sum(f"Hold strategy [{i}]" in prompt for prompt in judge) == 1


async def test_rejected_bootstrap_is_not_graded_as_a_hold(judge, capfire):
    result, _ = await invoke(
        TestModel(call_tools=[], custom_output_args={"action": "hold"}),
        initial_account={**account(), "cash": "999"},
        initial_portfolio=portfolio(),
    )
    await wait_for_evaluations()
    assert result.error is not None and result.decision is None
    assert judge == []
    [event] = evaluations(capfire)
    assert event["attributes"]["gen_ai.evaluation.score.label"] == "not_evaluated"
    assert "gen_ai.evaluation.score.value" not in event["attributes"]


async def test_order_evidence_is_judged_even_when_final_output_fails(judge, capfire):
    model, _ = script([ToolCallPart("market_order", order_request().model_dump(mode="json"))])
    result, requests = await invoke(
        model,
        payload=order(),
        overrides={"budget": DecisionBudget(model_requests=1)},
        instructions="Buy AAPL once.",
    )
    await wait_for_evaluations()
    assert result.decision is None and result.error is not None
    assert result.order_result.status == "filled" and len(requests) == 1
    [prompt] = judge
    assert "Buy AAPL once." in prompt and "filled" in prompt
    assert "order_request" in prompt and "quantity" in prompt and "buy" in prompt


async def test_code_mode_nested_research_reaches_judge(judge, capfire):
    model, _ = script(
        [code_call("result = " + news_code() + "\n'finished reading'")],
        lambda info: [output(info)],
    )
    result, _ = await invoke(model, payload=page([news()]), overrides=code_runtime())
    await wait_for_evaluations()
    assert result.error is None
    [prompt] = judge
    assert news()["headline"] in prompt and "research_observations" in prompt


async def test_session_drains_background_judge_before_the_loop_can_close(monkeypatch, capfire):
    from bazaar_agent import strategy_evaluation

    started, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        started.set()
        await release.wait()
        return httpx2.Response(200, json=verdict(0.1))

    route_judge(monkeypatch, slow)

    async def run():
        async with strategy_evaluation.strategy_evaluation_session():
            result, _ = await invoke(
                TestModel(call_tools=[], custom_output_args={"action": "hold"})
            )
            assert result.decision.action == "hold"  # returns while judge is still blocked
            await started.wait()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert not task.done()
    finally:
        release.set()
        await task
    assert len(evaluations(capfire)) == 3


async def test_disabled_session_never_dispatches_a_judge(monkeypatch, capfire):
    from bazaar_agent import strategy_evaluation

    def forbidden():
        pytest.fail("Disabled evaluation tried to build a judge")

    monkeypatch.setattr(strategy_evaluation, "judge_model", forbidden)
    async with asyncio.timeout(2), strategy_evaluation.strategy_evaluation_session():
        result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    assert result.error is None and evaluations(capfire) == []


async def test_judge_timeout_is_an_evaluation_error_and_session_finishes(monkeypatch, capfire):
    from bazaar_agent import strategy_evaluation

    async def never_finishes(request):
        await asyncio.Event().wait()

    route_judge(monkeypatch, never_finishes)
    monkeypatch.setattr(strategy_evaluation, "JUDGE_TIMEOUT_SECONDS", 0.01)
    async with asyncio.timeout(2), strategy_evaluation.strategy_evaluation_session():
        result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    assert result.error is None
    [event] = evaluations(capfire)
    assert event["attributes"]["error.type"] == "TimeoutError"


async def test_cancelled_decision_does_not_leave_session_waiting(judge, capfire):
    from bazaar_agent import strategy_evaluation

    async def cancelled(messages, info):
        raise asyncio.CancelledError

    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError):
            async with strategy_evaluation.strategy_evaluation_session():
                await invoke(FunctionModel(cancelled))
    assert judge == [] and evaluations(capfire) == []


async def test_the_judge_sees_the_summary_and_trading_day_the_agent_saw(judge, capfire):
    from datetime import date

    from .test_market_view import summarized_decision

    holdings = ({"symbol": "S003", "quantity": "2"},)
    result, _, summary, _, _ = await summarized_decision(
        holdings=holdings, trading_day=(1, 10, date(2026, 2, 2))
    )
    await wait_for_evaluations()
    assert result.error is None and summary
    [prompt] = judge
    # The judge reads the agent's own messages, so it sees the same summary and trading day.
    assert "MARKET SUMMARY" in prompt and "max_whole_shares=" in prompt
    assert "trading day 1 of 10 (first day 2026-02-02)" in prompt
    # The runner-side summary reads are context, not research the agent chose to do.
    assert '"research_observations":[]' in prompt


async def test_the_judges_gateway_call_carries_the_runs_trace(monkeypatch, capfire):
    """T3: the Gateway's Jev spans must join the run's trace, so the judge's httpx2 client sends
    traceparent for the active span; the Gateway key reaches no span."""
    import logfire

    seen = []

    def grade(request):
        seen.append(request)
        return httpx2.Response(200, json=verdict(0.9))

    route_judge(monkeypatch, grade)
    model, _ = script(lambda info: [output(info)])
    with logfire.span("runner.decision") as active:
        result, _ = await invoke(model)
        await wait_for_evaluations()
    assert result.error is None and len(seen) == 1
    traceparent = seen[0].headers.get("traceparent")
    assert traceparent is not None
    trace_id = f"{active.get_span_context().trace_id:032x}"
    assert traceparent.split("-")[1] == trace_id
    spans = capfire.exporter.exported_spans_as_dict()
    assert "test-gateway-key" not in json.dumps(spans, default=str)
    # The judge's own HTTP span is in the same trace.
    http = [s for s in spans if s["name"] == "POST"]
    assert http and all(f"{s['context']['trace_id']:032x}" == trace_id for s in http)


async def test_the_judges_client_is_closed_after_its_call(monkeypatch, capfire):
    from bazaar_agent import strategy_evaluation

    clients = []

    def new_client():
        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=verdict(0.9)))
        )
        clients.append(client)
        return client

    route_judge(monkeypatch, lambda request: None)
    monkeypatch.setattr(strategy_evaluation, "_new_http_client", new_client)
    model, _ = script(lambda info: [output(info)])
    await invoke(model)
    await wait_for_evaluations()
    assert len(clients) == 1 and clients[0].is_closed


async def test_a_failing_judge_is_logged_with_its_type_and_never_its_message(
    monkeypatch, capfire, caplog
):
    from bazaar_agent import strategy_evaluation

    def broken(http_client=None):
        raise RuntimeError("judge upstream said sk-not-a-real-secret-123")

    route_judge(monkeypatch, lambda request: httpx2.Response(200, json=verdict(0.9)))
    monkeypatch.setattr(strategy_evaluation, "judge_model", broken)
    with caplog.at_level("WARNING", logger="bazaar_agent.strategy_evaluation"):
        result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
        await wait_for_evaluations()
    assert result.error is None and result.decision.action == "hold"
    (record,) = [r for r in caplog.records if r.name == "bazaar_agent.strategy_evaluation"]
    assert record.levelname == "WARNING"
    assert record.getMessage() == (
        "strategy judge: 1 evaluator failure(s): StrategyAdherence (RuntimeError)"
    )
    assert "sk-not-a-real-secret" not in caplog.text


# T4: the Jev evidence guard. The judge never receives an input over the budget.


ARTICLE = " ".join(
    f"Synthetic market prose sentence {i} about supply and demand." for i in range(40)
)
assert 1900 < len(ARTICLE) < 2600


def heavy_market():
    """A real-shaped research-heavy decision: news and filings pages of ~2,000-character texts,
    and screen and quote rows, served through the real tool models."""
    from .test_market_view import FakeMarket
    from .test_research import filing, page

    def research(request):
        path = request.url.path
        symbol = path.rsplit("/", 1)[-1]
        if "/news/" in path:
            items = [
                news(
                    record_id=f"{symbol}-n{i}",
                    symbol=symbol,
                    headline=f"{symbol} headline {i}",
                    text=ARTICLE,
                )
                for i in range(5)
            ]
            return httpx.Response(200, json=page(items))
        if "/filings/" in path:
            return httpx.Response(
                200, json=page([filing(record_id=f"{symbol}-f", text=ARTICLE * 2)])
            )
        return httpx.Response(404)

    return FakeMarket(size=60, other=research)


def heavy_script():
    return script(
        [ToolCallPart("news", query(symbol="AAPL"))],
        [ToolCallPart("screen", {"limit": 50})],
        [ToolCallPart("quotes", {"symbols": [f"S{i:03d}" for i in range(20)]})],
        [ToolCallPart("filings", query(symbol="AAPL"))],
        [ToolCallPart("news", query(symbol="MSFT"))],
        lambda info: [output(info)],
    )


@pytest.fixture
def guarded(monkeypatch, capfire):
    """Runs the heavy decision through the production judge path with a given budget; returns
    what the judge was sent (each request's state size and state) and the evaluation events."""
    from bazaar_agent import strategy_evaluation
    from pydantic_core import to_json

    sent = []
    captured = {}

    def grade(request):
        body = json.loads(request.content)
        sent.append((len(to_json(body["state"])), body["state"]))
        return httpx2.Response(200, json=verdict(0.9))

    route_judge(monkeypatch, grade)
    real_fit = strategy_evaluation.fit_evidence

    async def spy(evidence, size, budget):
        captured.update(evidence=evidence, size=size)
        return await real_fit(evidence, size, budget)

    monkeypatch.setattr(strategy_evaluation, "fit_evidence", spy)

    async def run(budget):
        sent.clear()
        monkeypatch.setenv("BAZAAR_JUDGE_INPUT_BUDGET_CHARS", str(budget))
        model, _ = heavy_script()
        result, _ = await invoke(
            model,
            handler=heavy_market(),
            overrides={
                "budget": DecisionBudget(model_requests=10, tool_calls=20, total_tokens=100_000)
            },
        )
        await wait_for_evaluations()
        events = {
            e["attributes"]["gen_ai.evaluation.name"]: e["attributes"] for e in evaluations(capfire)
        }
        capfire.log_exporter.clear()
        return result, list(sent), events

    run.captured = captured
    return run


async def sizes(captured):
    """The exact judge-input size after each trim step, as the guard itself measures it."""
    from bazaar_agent.strategy_evaluation import marked, trim_market_rows, trim_news

    evidence, size = captured["evidence"], captured["size"]
    news_only = marked(trim_news(evidence), ["news"])
    rows_too = marked(trim_market_rows(trim_news(evidence)), ["news", "market_rows"])
    return await size(evidence), await size(news_only), await size(rows_too)


async def test_an_oversized_decision_is_trimmed_only_as_far_as_it_must(guarded):
    _, sent, events = await guarded(10**9)
    # Under budget: sent unchanged, with no truncation fields anywhere.
    assert len(sent) == 1 and "strategy_adherence_evidence_truncated" not in events
    assert "evidence_truncated" not in json.dumps(sent[0][1])
    full, after_news, after_rows = await sizes(guarded.captured)
    print(f"judge input bytes: full {full}, after news {after_news}, after rows {after_rows}")
    assert full > after_news > after_rows  # each step really shrinks the input
    assert full > 60_000  # real-shaped and oversized for the production budget

    # Fits after the news step: only that step.
    budget = (full + after_news) // 2
    _, sent, events = await guarded(budget)
    ((sent_size, state),) = sent
    assert sent_size <= budget
    # The judge is told, in the input it sees, that and how the evidence was trimmed.
    text = json.dumps(state)
    assert "evidence_truncated" in text and "evidence_trim_steps" in text
    assert (
        events["strategy_adherence_evidence_trim_steps"]["gen_ai.evaluation.score.label"] == "news"
    )
    assert "strategy_adherence" in events

    # Needs market rows too.
    budget = (after_news + after_rows) // 2
    _, sent, events = await guarded(budget)
    ((sent_size, state),) = sent
    assert sent_size <= budget
    label = events["strategy_adherence_evidence_trim_steps"]["gen_ai.evaluation.score.label"]
    assert label == "news,market_rows"

    # Needs old results dropped as well: still within the budget, and all three steps in order.
    budget = after_rows - 15_000
    _, sent, events = await guarded(budget)
    ((sent_size, state),) = sent
    assert sent_size <= budget
    label = events["strategy_adherence_evidence_trim_steps"]["gen_ai.evaluation.score.label"]
    assert label == "news,market_rows,observations"
    text = json.dumps(state)
    assert "result dropped to fit" in text
    # Oldest first, only as many as needed: the newest research (MSFT news) is still there.
    assert "MSFT headline" in text and "AAPL headline" not in text
    assert events["strategy_adherence_evidence_truncated"]["gen_ai.evaluation.score.value"] in (
        True,
        1,
        1.0,
    )


async def test_evidence_that_cannot_fit_is_not_sent(guarded):
    result, sent, events = await guarded(2_000)  # smaller than the rubric and strategy alone
    assert sent == []  # the judge is never called
    status = events["strategy_adherence_status"]
    assert status["gen_ai.evaluation.score.label"] == "not_evaluated"
    assert status["gen_ai.evaluation.explanation"] == "evidence too large for the judge"
    assert result.error is None  # the decision itself is untouched


async def test_trimming_never_changes_the_agents_evidence(guarded):
    from bazaar_agent.strategy_evaluation import fit_evidence

    await guarded(10**9)
    evidence, size = guarded.captured["evidence"], guarded.captured["size"]
    before = json.dumps(evidence, sort_keys=True)
    fitted = await fit_evidence(evidence, size, (await size(evidence)) // 3)
    assert fitted is not None and fitted[0] is not evidence
    assert json.dumps(evidence, sort_keys=True) == before
    assert "evidence_truncated" not in evidence


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
async def test_the_budget_setting_is_validated(monkeypatch, value):
    from bazaar_agent import strategy_evaluation
    from pydantic_ai.exceptions import UserError

    monkeypatch.setenv("BAZAAR_JUDGE_INPUT_BUDGET_CHARS", value)
    with pytest.raises(UserError, match="BAZAAR_JUDGE_INPUT_BUDGET_CHARS"):
        strategy_evaluation.judge_input_budget()


def test_the_budget_defaults_to_55000_and_reads_the_setting(monkeypatch):
    from bazaar_agent import strategy_evaluation

    monkeypatch.delenv("BAZAAR_JUDGE_INPUT_BUDGET_CHARS", raising=False)
    assert strategy_evaluation.judge_input_budget() == 55_000
    monkeypatch.setenv("BAZAAR_JUDGE_INPUT_BUDGET_CHARS", " 70000 ")
    assert strategy_evaluation.judge_input_budget() == 70_000


async def test_the_real_judge_never_sends_an_input_over_its_budget(monkeypatch):
    """The backstop: even unsized, a request whose state is over the budget is not sent."""
    from bazaar_agent import strategy_evaluation
    from pydantic_ai.models.decision import DecisionRequest

    sent = []
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_BASE_URL", GATEWAY)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: sent.append(r)))
    judge = strategy_evaluation.judge_model(client)
    judge.input_budget = 100
    with pytest.raises(strategy_evaluation.JudgeInputTooLarge) as caught:
        await judge.decide(DecisionRequest(state="x" * 500, questions={}), {})
    assert caught.value.size == 502 and sent == []  # the JSON string, quotes included
    await client.aclose()


def test_judge_model_needs_the_callers_traced_client():
    """No default client: one could go untraced (no traceparent) or never be closed."""
    import inspect

    from bazaar_agent import strategy_evaluation

    parameter = inspect.signature(strategy_evaluation.judge_model).parameters["http_client"]
    assert parameter.default is inspect.Parameter.empty
