"""Exercise real online evaluation and OTel emission with local judge models."""

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
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


@pytest.fixture
def judge(monkeypatch):
    # Production must pass only decision evidence to the judge, never runtime handles.
    from bazaar_agent import strategy_evaluation

    prompts = []

    def grade(messages, info):
        prompts.append(
            "\n".join(
                str(part.content)
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "pass": True,
                        "score": 1.0,
                        "reason": "The agent held as the supplied strategy required.",
                    },
                )
            ]
        )

    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.setattr(strategy_evaluation, "judge_model", lambda: FunctionModel(grade))
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
    assert score["gen_ai.evaluation.explanation"] == (
        "The agent held as the supplied strategy required."
    )
    assert (
        by_name["strategy_adherence_pass"]["attributes"]["gen_ai.evaluation.score.label"] == "pass"
    )
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


async def test_failed_judge_does_not_change_a_decision(monkeypatch, judge, capfire):
    from bazaar_agent import strategy_evaluation

    def broken(messages, info):
        raise RuntimeError("judge unavailable")

    monkeypatch.setattr(strategy_evaluation, "judge_model", lambda: FunctionModel(broken))
    result, _ = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    await wait_for_evaluations()
    assert result.error is None and result.decision.action == "hold"
    [event] = evaluations(capfire)
    assert event["attributes"]["error.type"] == "RuntimeError"
    assert "gen_ai.evaluation.score.value" not in event["attributes"]


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

    async def slow(messages, info):
        started.set()
        await release.wait()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "pass": False,
                        "score": 0.0,
                        "reason": "Holding violated the buy instruction.",
                    },
                )
            ]
        )

    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.setattr(strategy_evaluation, "judge_model", lambda: FunctionModel(slow))

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
    assert len(evaluations(capfire)) == 2


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

    async def never_finishes(messages, info):
        await asyncio.Event().wait()

    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.setattr(strategy_evaluation, "JUDGE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(strategy_evaluation, "judge_model", lambda: FunctionModel(never_finishes))
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
