"""Submissions trace the agent with content; every span carries bazaar.*; no secret leaks."""

import json

import pytest
from bazaar_runner import submission
from bazaar_runner.agent import fixture_model_factory
from bazaar_runner.submission import run_submission, submission_experiment_id
from pydantic_ai.models.function import FunctionModel

from .market_fakes import TOKEN
from .test_submission import INSTRUCTIONS, GrantingMarket

SENTINELS = {
    "PYDANTIC_AI_GATEWAY_API_KEY": "sentinel-gateway-key-0f1e",
    "BAZAAR_ADMIN_TOKEN": "sentinel-admin-token-2d3c",
    "LOGFIRE_TOKEN": "sentinel-logfire-token-4b5a",
}


@pytest.fixture
def traced_submission(monkeypatch, capfire, tmp_path):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BAZAAR_RUNNER_TOKEN", TOKEN)
    market = GrantingMarket()
    transport = market.transport()
    monkeypatch.setattr(submission, "_transport_for", lambda url: transport)
    monkeypatch.setattr(submission, "configure_telemetry", lambda: None)
    seen_messages: list = []

    def recording_factory(model):
        fixture = fixture_model_factory()

        def build(model_ref):
            inner = fixture(model_ref)

            def call(messages, info):
                seen_messages.append(messages)
                return inner.function(messages, info)

            return FunctionModel(call)

        return build

    monkeypatch.setattr(submission, "_model_factory", recording_factory)
    run_dir = run_submission(
        submission_id="obs-1",
        name="news-reader",
        instructions=INSTRUCTIONS,
        market_url="http://market",
        runner_token=TOKEN,
        runs_dir=tmp_path,
        handle="@attendee",
    )
    spans = capfire.exporter.exported_spans_as_dict()
    return run_dir, spans, seen_messages


def named(spans, prefix):
    return [s for s in spans if s["name"].startswith(prefix)]


def test_every_span_of_the_run_carries_the_bazaar_attributes(traced_submission):
    _, spans, _ = traced_submission
    (run,) = named(spans, "runner.run")
    expected = {
        "bazaar.strategy_name": "news-reader",
        "bazaar.submission_id": "obs-1",
        "bazaar.experiment_id": str(submission_experiment_id("obs-1")),
        "bazaar.policy_kind": "agent",
        "bazaar.handle": "@attendee",
    }
    trace = run["context"]["trace_id"]
    in_run = [s for s in spans if s["context"]["trace_id"] == trace]
    assert len(in_run) > 50
    for span in in_run:
        attributes = span["attributes"]
        assert {k: attributes.get(k) for k in expected} == expected, span["name"]
        assert attributes.get("bazaar.run_id") == run["attributes"]["bazaar.run_id"]


def test_the_agent_run_is_traced_with_content_and_usage(traced_submission):
    _, spans, _ = traced_submission
    text = json.dumps(spans, default=str)
    # Agent, model request and tool call spans now exist under the decisions.
    assert any("gen_ai.request.model" in s["attributes"] for s in spans)
    assert any(k.startswith("gen_ai.usage.") for s in spans for k in s["attributes"]), (
        "model request spans carry token usage"
    )
    assert any("market_order" in s["name"] for s in spans), "tool call spans"
    # Message content is included: the submitter's strategy text reaches the trace.
    assert "buy ten AAPL" in text


def test_decisions_and_the_scored_log_carry_performance(traced_submission):
    _, spans, _ = traced_submission
    decisions = named(spans, "runner.decision")
    assert len(decisions) == 10
    first = next(d for d in decisions if d["attributes"]["event_sequence"] == 0)
    assert {
        k: first["attributes"][k] for k in ("bazaar.action", "bazaar.symbol", "bazaar.side")
    } == {
        "bazaar.action": "ordered",
        "bazaar.symbol": "AAPL",
        "bazaar.side": "buy",
    }
    assert first["attributes"]["bazaar.quantity"] == "10"
    assert "bazaar.fill_price" in first["attributes"]
    assert {d["attributes"]["bazaar.action"] for d in decisions if d is not first} == {"hold"}
    (scored,) = named(spans, "bazaar.run scored")
    attributes = scored["attributes"]
    assert attributes["fills"] == 1 and attributes["decisions_exhausted"] == 0
    assert isinstance(attributes["return_pct"], float)
    assert isinstance(attributes["ending_value"], float)
    assert "excess_vs_buy_and_hold_pct" not in attributes
    assert attributes["bazaar.submission_id"] == "obs-1"


def test_no_secret_reaches_any_span_or_the_agent(traced_submission):
    run_dir, spans, messages = traced_submission
    exported = json.dumps(spans, default=str)
    for secret in (*SENTINELS.values(), TOKEN):
        assert secret not in exported
    assert messages, "the model was called"
    assert TOKEN not in json.dumps([repr(m) for m in messages])
    for path in run_dir.rglob("*"):
        assert TOKEN not in path.read_text()
