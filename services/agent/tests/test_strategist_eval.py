"""The strategist experiment (v10 D1): evaluators, the task and telemetry isolation. Offline only:
FunctionModel stands in for the model; invented strategies and symbols."""

import json
from datetime import UTC, datetime
from typing import ClassVar

import logfire
import pytest
from bazaar_agent import strategist, strategist_eval
from bazaar_agent.strategist import (
    Draft,
    PerformanceBrief,
    Proposal,
    StrategistError,
    StrategyStats,
)
from bazaar_agent.strategist_eval import (
    PROMPT_VARIANT,
    DeliveredK,
    DistinctNames,
    Executable,
    LearnsFromTop,
    NoMultiPosition,
    NoParentCopy,
    ParentDistance,
    ParentsInBrief,
    SectorDiversity,
    StrategistCase,
    StrategistCaseMeta,
    StrategistOutput,
    TickerDiversity,
    TopParentShare,
    dataset,
    make_task,
    push_dataset,
)
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_evals.evaluators import EvaluatorContext


def stats(name, return_pct):
    return StrategyStats(
        name=name,
        runs=2,
        return_pct=return_pct,
        excess_vs_benchmark_pct=return_pct - 1,
        fills=2,
        llm_cost_usd=0.05,
        adherence=None,
        budget_exhausted=0,
        policy_kind="agent",
    )


MOMO = "Buy the strongest Fixture Tech stock each morning with 30% of cash; sell on a down day."
CASE = StrategistCase(
    brief=PerformanceBrief(
        bundle_id="demo-bundle-v2",
        since=datetime(2026, 10, 11, 20, 14, tzinfo=UTC),
        generated_at=datetime(2026, 10, 12, 14, 0, tzinfo=UTC),
        benchmark_return_pct=1.0,
        runs_seen=6,
        top=[stats("momo-a", 3.0)],
        bottom=[stats("value-c", -1.0)],
        max_generation_seen=None,
    ),
    k=2,
    generation=1,
    instructions_by_name={"momo-a": MOMO, "value-c": "Buy KO after any 2% drop; hold 5 days."},
    existing_instructions=[],
    universe=[
        {"symbol": "AAPL", "name": "Apple Fixture", "sector": "Fixture Tech"},
        {"symbol": "KO", "name": "Coke Fixture", "sector": "Fixture Staples"},
    ],
    benchmark="SPY",
)


META = StrategistCaseMeta(
    parent_texts="synthetic",
    source="brief: production since 2026-10-11T20:14Z; parent texts: written for the eval",
)
DESCRIPTION = "Real brief stats with SYNTHETIC parent texts (written for the eval)."


def draft(name, instructions, parents=("momo-a",)):
    return Draft(name=name, instructions=instructions, parents=list(parents), idea="An idea.")


GOOD = draft("g1-dip", "Buy AAPL after two down days with 25% of cash; sell after 3 days.")
MULTI = draft("g1-spread", "Hold the top 8 Fixture Tech stocks at 12% of equity each.")
TESLA = draft("g1-tesla", "Buy TSLA whenever it rises two days in a row; hold a week.")
OUTSIDER = draft("g1-orphan", "Buy KO on any up day and sell it on any down day.", ["nope"])
MIXED = draft("g1-mixed", "Buy KO on any up day and sell it on any down day!", ["momo-a", "x"])
COPY = draft("g1-copy", MOMO.replace("strongest", "strongst"))


def score(evaluator, drafts, proposals=(), k=2):
    output = StrategistOutput(drafts=list(drafts), proposals=list(proposals), k=k)
    ctx = EvaluatorContext(
        name="case",
        inputs=CASE,
        metadata=None,
        expected_output=None,
        output=output,
        duration=0.0,
        _span_tree=None,
        attributes={},
        metrics={},
    )
    return evaluator.evaluate(ctx)


def test_executable_scores_the_raw_drafts_a_trader_can_run():
    assert score(Executable(), [GOOD]) == 1.0
    assert score(Executable(), [GOOD, MULTI, TESLA, COPY]) == 0.5  # multi and ticker fail
    assert score(Executable(), []) == 0.0


def test_no_multi_position():
    assert score(NoMultiPosition(), [GOOD, MULTI]) == 0.5


def test_distinct_names():
    duplicate = draft("dip", "Buy KO after two down days; sell after 3 days.")  # g1-dip again
    brief_name = draft("momo-a", "Buy KO after two down days; sell after 3 days.")
    invalid = draft("Bad Name!", "Buy KO after two down days; sell after 3 days.")
    assert score(DistinctNames(), [GOOD, MULTI]) == 1.0
    assert score(DistinctNames(), [GOOD, duplicate, MULTI]) == pytest.approx(1 / 3)
    # An unprefixed draft name gets g1-, so "momo-a" is g1-momo-a, not the brief's momo-a.
    assert score(DistinctNames(), [invalid]) == 0.0
    taken = draft("g1-taken", "Buy KO after two down days; sell after 3 days.")
    case = CASE.model_copy(
        update={"brief": CASE.brief.model_copy(update={"bottom": [stats("g1-taken", -2.0)]})}
    )
    ctx = EvaluatorContext(
        name="case",
        inputs=case,
        metadata=None,
        expected_output=None,
        output=StrategistOutput(drafts=[taken, brief_name], proposals=[], k=2),
        duration=0.0,
        _span_tree=None,
        attributes={},
        metrics={},
    )
    assert DistinctNames().evaluate(ctx) == 0.5


def test_parents_in_brief_needs_every_listed_parent():
    no_parents = draft("g1-none", "Buy KO on any up day and sell it on any down day.", [])
    assert score(ParentsInBrief(), [GOOD]) == 1.0
    assert score(ParentsInBrief(), [OUTSIDER, MIXED, no_parents, GOOD]) == 0.25


def test_no_parent_copy():
    assert score(NoParentCopy(), [GOOD, COPY]) == 0.5


def test_delivered_k_is_an_assertion_on_the_requested_k():
    proposal = Proposal(name="g1-dip", instructions=GOOD.instructions, parents=["momo-a"], idea="i")
    assert score(DeliveredK(), [GOOD], [proposal], k=1) is True
    assert score(DeliveredK(), [GOOD], [proposal], k=2) is False


def model_returning(drafts, roles=None):
    async def respond(messages, info):
        if roles is not None:
            roles.append(messages[0].instructions)
        payload = {"drafts": [d.model_dump() for d in drafts]}
        return ModelResponse([ToolCallPart(info.output_tools[0].name, payload)])

    return FunctionModel(respond)


@pytest.fixture
def with_model(monkeypatch):
    built = []

    def install(model):
        def build(model_name=None):
            built.append(model_name)
            return model

        monkeypatch.setattr(strategist, "build_model", build)
        return built

    return install


async def test_the_task_returns_raw_drafts_and_proposals(with_model):
    roles = []
    built = with_model(model_returning([MULTI, GOOD, TESLA, MIXED], roles))
    output = await make_task(role=PROMPT_VARIANT, model="gateway/openai:some-model")(CASE)
    assert [d.name for d in output.drafts] == ["g1-spread", "g1-dip", "g1-tesla", "g1-mixed"]
    assert [p.name for p in output.proposals] == ["g1-dip", "g1-mixed"] and output.k == 2
    assert built == ["gateway/openai:some-model"] and roles == [PROMPT_VARIANT]
    assert "UNTRUSTED DATA" in PROMPT_VARIANT and PROMPT_VARIANT.startswith(
        strategist.STRATEGIST_ROLE
    )


async def test_the_task_k_overrides_the_cases(with_model):
    built = with_model(model_returning([GOOD, MIXED]))
    output = await make_task(k=1)(CASE)
    assert [p.name for p in output.proposals] == ["g1-dip"] and output.k == 1
    assert built == [None]  # no model name: the strategist's default


def judge_returning(score=0.8, prompts=None):
    async def respond(messages, info):
        if prompts is not None:
            prompts.append(
                "\n".join(
                    str(p.content)
                    for m in messages
                    for p in m.parts
                    if isinstance(p, UserPromptPart)
                )
            )
        grade = {"reason": "Keeps the top's momentum trait.", "pass": True, "score": score}
        return ModelResponse([ToolCallPart(info.output_tools[0].name, grade)])

    # LearnsFromTop instruments it as it would a real judge (build_judge).
    return FunctionModel(respond)


def test_an_experiment_never_looks_like_a_run_or_a_proposal(with_model, capfire):
    """PM: experiment output, the judge's calls included, must never reach dashboards or brief()."""
    with_model(model_returning([MULTI, GOOD, MIXED]))
    report = dataset({"two-strategies": (CASE, META)}, judge_model=judge_returning()).evaluate_sync(
        make_task(), name="role-baseline", progress=False
    )
    (case,) = report.cases
    assert case.assertions["DeliveredK"].value is True
    assert case.scores["NoMultiPosition"].value == pytest.approx(2 / 3)
    assert case.scores["LearnsFromTop"].value == 0.8 and not case.evaluator_failures

    spans = capfire.exporter.exported_spans_as_dict()
    names = [s["name"] for s in spans]
    messages = [s["attributes"].get("logfire.msg") for s in spans]
    assert "evaluate role-baseline" in messages and "strategist.eval_case" in names
    assert any(name.startswith("chat ") for name in names)  # the judge's model call
    assert "bazaar.run scored" not in names and "bazaar.run scored" not in messages
    assert "strategist.propose" not in names
    for span in spans:
        assert not {"bazaar.strategy_name", "bazaar.run_id"} & set(span["attributes"])
    assert "bazaar.run_id" not in json.dumps(spans, default=str)


class FakeClient:
    instances: ClassVar[list["FakeClient"]] = []

    def __init__(self, api_key=None, **kwargs):
        self.api_key, self.pushed = api_key, []
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def push_dataset(self, ds, **kwargs):
        self.pushed.append((ds, kwargs))
        return {"name": ds.name}


@pytest.fixture
def fake_client(monkeypatch):
    from logfire.experimental import api_client

    FakeClient.instances = []
    monkeypatch.setattr(api_client, "LogfireAPIClient", FakeClient)
    return FakeClient


def test_push_without_a_key_skips_and_builds_no_client(fake_client, monkeypatch):
    monkeypatch.delenv(strategist_eval.WRITE_KEY_ENV, raising=False)
    assert (
        push_dataset(dataset({"c": (CASE, META)}), description=DESCRIPTION)
        == "skipped: LOGFIRE_API_WRITE_KEY not set"
    )
    assert fake_client.instances == []


def test_push_with_a_key_sends_the_dataset(fake_client, monkeypatch):
    monkeypatch.setenv(strategist_eval.WRITE_KEY_ENV, "fake-write-key-for-tests")
    ds = dataset({"c": (CASE, META)})
    result = push_dataset(ds, description=DESCRIPTION)
    (client,) = fake_client.instances
    assert client.api_key == "fake-write-key-for-tests" and client.pushed == [
        (ds, {"description": DESCRIPTION})
    ]
    assert result == "pushed bazaar-strategist (1 cases)" and "fake-write-key" not in result


def test_a_push_failure_names_only_its_type(fake_client, monkeypatch):
    monkeypatch.setenv(strategist_eval.WRITE_KEY_ENV, "fake-write-key-for-tests")

    def boom(self, ds, **kwargs):
        raise RuntimeError("denied for fake-write-key-for-tests")

    monkeypatch.setattr(FakeClient, "push_dataset", boom)
    with pytest.raises(StrategistError, match=r"\(RuntimeError\)$") as caught:
        push_dataset(dataset({"c": (CASE, META)}), description=DESCRIPTION)
    assert "fake-write-key" not in str(caught.value) and caught.value.__context__ is None


def test_the_dataset_carries_every_evaluator_and_named_cases():
    real = StrategistCaseMeta(parent_texts="real", source="parent texts: bazaar-strategies")
    ds = dataset({"a": (CASE, META), "b": (CASE.model_copy(update={"k": 3}), real)})
    assert ds.name == "bazaar-strategist" and [c.name for c in ds.cases] == ["a", "b"]
    assert {type(e).__name__ for e in ds.evaluators} == {
        "Executable",
        "NoMultiPosition",
        "DistinctNames",
        "ParentsInBrief",
        "NoParentCopy",
        "DeliveredK",
        "TopParentShare",
        "TickerDiversity",
        "SectorDiversity",
        "ParentDistance",
        "LearnsFromTop",
    }
    (judge,) = [e for e in ds.evaluators if type(e).__name__ == "LearnsFromTop"]
    assert judge.model == "gateway/anthropic:claude-sonnet-5"  # one fixed judge for every arm
    assert "LearnsFromTop" not in {
        type(e).__name__ for e in dataset({"a": (CASE, META)}, judge_model=None).evaluators
    }


def test_case_meta_round_trips_through_the_dataset(tmp_path):
    real = StrategistCaseMeta(parent_texts="real", source="parent texts: bazaar-strategies")
    ds = dataset({"synthetic-parents": (CASE, META), "real-parents": (CASE, real)})
    assert [c.metadata for c in ds.cases] == [META, real]
    assert [c.inputs for c in ds.cases] == [CASE, CASE]
    # Through a file and back, typed again by the dataset's generics.
    path = tmp_path / "bazaar-strategist.json"
    evaluator_types = [type(e) for e in ds.evaluators]
    ds.to_file(path, custom_evaluator_types=evaluator_types)
    again = type(ds).from_file(path, custom_evaluator_types=evaluator_types)
    assert [c.metadata for c in again.cases] == [META, real]
    assert isinstance(again.cases[0].metadata, StrategistCaseMeta)
    assert again.cases[0].inputs == CASE
    with pytest.raises(ValueError):
        StrategistCaseMeta(parent_texts="made-up", source="x")


# Quality scores (D1-S2): computed on the ACCEPTED proposals.


def proposal(name, instructions, parents=("momo-a",)):
    return Proposal(name=name, instructions=instructions, parents=list(parents), idea="An idea.")


def quality(evaluator, proposals):
    return score(evaluator, [], proposals, k=len(proposals))


def test_top_parent_share():
    links = [
        proposal("g1-a", "Buy AAPL on up days.", ["momo-a"]),
        proposal("g1-b", "Buy KO on up days.", ["momo-a"]),
        proposal("g1-c", "Buy KO on down days.", ["value-c"]),
    ]
    assert quality(TopParentShare(), links) == pytest.approx(2 / 3)
    assert quality(TopParentShare(), links[:2]) == 1.0
    assert quality(TopParentShare(), []) == 0.0


def test_ticker_diversity_reads_tickers_as_the_gate_does():
    same = [proposal(f"g1-{i}", "Buy AAPL when RSI < 30; sell AAPL after 3 days.") for i in "abc"]
    assert quality(TickerDiversity(), same) == pytest.approx(1 / 6)  # RSI is not a ticker
    spread = [
        proposal("g1-a", "Buy AAPL on up days."),
        proposal("g1-b", "Buy KO on up days."),
        proposal("g1-c", "Buy $AAPL or KO on down days."),
    ]
    assert quality(TickerDiversity(), spread) == 0.5  # 2 distinct / 4 mentions
    assert quality(TickerDiversity(), [proposal("g1-x", "Buy the top mover each day.")]) == 0.0


def test_sector_diversity():
    tech = [proposal(f"g1-{i}", "Buy AAPL on up days.") for i in "abc"]
    assert quality(SectorDiversity(), tech) == 0.5  # 1 sector / min(3 proposals, 2 sectors)
    both = [proposal("g1-a", "Buy AAPL on up days."), proposal("g1-b", "Buy KO on up days.")]
    assert quality(SectorDiversity(), both) == 1.0
    # The benchmark is its own bucket; the score is capped at 1.
    spy = [proposal("g1-a", "Buy SPY and AAPL and KO on up days.")]
    assert quality(SectorDiversity(), spy) == 1.0
    assert quality(SectorDiversity(), []) == 0.0


def test_parent_distance():
    copy = proposal("g1-copy", MOMO)
    fresh = proposal("g1-fresh", "Hold KO through earnings week and sell on the first gap down.")
    orphan = proposal("g1-orphan", "Buy KO on up days.", ["no-text"])
    assert quality(ParentDistance(), [copy]) == 0.0
    far = quality(ParentDistance(), [fresh])
    assert 0.5 < far < 1.0
    # A proposal whose parents have no text is skipped, not counted as 0 or 1.
    assert quality(ParentDistance(), [copy, fresh, orphan]) == pytest.approx(far / 2)
    assert quality(ParentDistance(), [orphan]) == 0.0


async def test_the_judge_scores_from_a_compact_prompt_without_the_universe():
    prompts = []
    universe = [
        {"symbol": f"S{i:03d}", "name": f"Stock {i}", "sector": "Fixture Tech"} for i in range(500)
    ]
    case = CASE.model_copy(update={"universe": [*CASE.universe, *universe]})
    accepted = [proposal("g1-a", "Buy AAPL on up days with 25% of cash.")]
    ctx = EvaluatorContext(
        name="case",
        inputs=case,
        metadata=META,
        expected_output=None,
        output=StrategistOutput(drafts=[], proposals=accepted, k=1),
        duration=0.0,
        _span_tree=None,
        attributes={},
        metrics={},
    )
    result = await LearnsFromTop(model=judge_returning(0.7, prompts)).evaluate(ctx)
    assert result.value == 0.7 and result.reason == "Keeps the top's momentum trait."
    (prompt,) = prompts
    assert len(prompt) < 3000 and "S499" not in prompt and "Stock 1" not in prompt
    assert "Apple Fixture" not in prompt and "UNTRUSTED" in prompt
    assert MOMO in prompt and "Buy AAPL on up days with 25% of cash." in prompt
    assert "keep what distinguishes the top performers" in prompt  # the rubric


def test_a_judge_failure_is_recorded_and_the_experiment_goes_on(with_model):
    async def boom(messages, info):
        raise RuntimeError("upstream said sk-secret-value")

    with_model(model_returning([GOOD, MIXED]))
    report = dataset({"c": (CASE, META)}, judge_model=FunctionModel(boom)).evaluate_sync(
        make_task(), progress=False
    )
    (case,) = report.cases
    (failure,) = case.evaluator_failures
    assert failure.name == "LearnsFromTop" and "RuntimeError" in failure.error_message
    assert "sk-secret" not in failure.error_message + failure.error_stacktrace
    # Every other score was still computed.
    assert case.scores["TopParentShare"].value == 1.0 and "LearnsFromTop" not in case.scores


async def test_the_judge_has_room_to_answer_and_is_instrumented(monkeypatch, capfire):
    """Real run 2026-10-12: at max_tokens 600 the judge was cut off on every case, and its calls
    had no chat span (no cost)."""
    from bazaar_agent.redaction import RedactingModel
    from pydantic_ai.models.instrumented import InstrumentedModel

    seen = []

    async def respond(messages, info):
        seen.append(info.model_settings)
        grade = {"reason": "ok", "pass": True, "score": 0.5}
        return ModelResponse([ToolCallPart(info.output_tools[0].name, grade)])

    assert strategist_eval.JUDGE_SETTINGS["max_tokens"] >= 4000
    built = strategist_eval.build_judge(FunctionModel(respond))
    assert isinstance(built, InstrumentedModel) and isinstance(built.wrapped, RedactingModel)
    assert built.instrumentation_settings is strategist.STRATEGIST_INSTRUMENTATION
    # A name is built on the strategist's own path.
    names = []
    monkeypatch.setattr(strategist, "build_model", lambda name=None: names.append(name) or built)
    assert strategist_eval.build_judge("gateway/anthropic:claude-sonnet-5") is built
    assert names == ["gateway/anthropic:claude-sonnet-5"]

    accepted = [proposal("g1-a", "Buy AAPL on up days with 25% of cash.")]
    ctx = EvaluatorContext(
        name="case",
        inputs=CASE,
        metadata=META,
        expected_output=None,
        output=StrategistOutput(drafts=[], proposals=accepted, k=1),
        duration=0.0,
        _span_tree=None,
        attributes={},
        metrics={},
    )
    with logfire.span("evaluator LearnsFromTop"):
        assert (await LearnsFromTop(model=FunctionModel(respond)).evaluate(ctx)).value == 0.5
    assert seen and all(s["max_tokens"] >= 4000 for s in seen)
    spans = capfire.exporter.exported_spans_as_dict()
    (evaluator,) = [s for s in spans if s["name"] == "evaluator LearnsFromTop"]
    (chat,) = [s for s in spans if s["name"].startswith("chat ")]
    # The judge's model call is a chat span inside the evaluator's trace.
    assert chat["context"]["trace_id"] == evaluator["context"]["trace_id"]
    assert (
        evaluator["start_time"] <= chat["start_time"] <= chat["end_time"] <= evaluator["end_time"]
    )
