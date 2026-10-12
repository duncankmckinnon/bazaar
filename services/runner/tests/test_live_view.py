"""The Logfire live view (v11 S3): every Bazaar span's message leads with the strategy name, while
every span NAME stays exactly what dashboards, alerts and brief() match on."""

import pytest
from bazaar_evaluation import evaluate_and_emit
from bazaar_runner.record import record_run

from .market_fakes import SPEC, InMemoryMarket
from .test_observability import traced_submission  # noqa: F401 - the agent submission fixture
from .test_record import buy_once

# Captured from origin/main ecc0410 before this change; a renamed span fails here.
FIXED_RUN_SPAN_NAMES = {
    "bazaar.run scored",
    "evaluate run {experiment_id}",
    "runner.decision",
    "runner.evaluate",
    "runner.mark",
    "runner.order",
    "runner.run",
    "trade {symbol} {side} {status}",
}
SUBMISSION_SPAN_NAMES = {
    "GET",
    "POST",
    "PUT",
    "Resolve variable bazaar_trading_role",
    "account tool",
    "bazaar.run scored",
    "chat function:call:",
    "evaluate run {experiment_id}",
    "execute_tool market_order",
    "execute_tool news",
    "invoke_agent simulated-stock-trader",
    "news tool",
    "order tool",
    "portfolio tool",
    "quotes tool",
    "research.tool",
    "runner.decision",
    "runner.evaluate",
    "runner.mark",
    "runner.run",
    "screen tool",
    "sectors tool",
    "trade {symbol} {side} {status}",
    "trading.decision",
}
# The spans this slice labels, with the rest of each message after "<strategy> · ".
LABELLED = {
    "runner.run": "run",
    "runner.decision": "day 1 · decide",
    "runner.mark": "day 1 · mark",
    "runner.order": "order buy 5 AAPL",
    "runner.evaluate": "evaluate",
    "evaluate run {experiment_id}": f"evaluate run {SPEC.experiment_id}",
    "trade {symbol} {side} {status}": "trade AAPL buy ",
}


async def fixed_run(tmp_path, strategy_name):
    await record_run(
        SPEC,
        InMemoryMarket(),
        buy_once,
        policy_ref="scripted-test",
        runs_dir=tmp_path,
        evaluate=evaluate_and_emit,
        strategy_name=strategy_name,
    )


def first(spans, name):
    return min(
        (s for s in spans if s["name"] == name),
        key=lambda s: s["attributes"].get("event_sequence", 0),
    )


async def test_the_span_names_are_pinned(capfire, tmp_path):
    await fixed_run(tmp_path, "tech-momentum")
    names = {s["name"] for s in capfire.exporter.exported_spans_as_dict()}
    assert names == FIXED_RUN_SPAN_NAMES


def test_the_agent_submissions_span_names_are_pinned(traced_submission):  # noqa: F811
    _, spans, _ = traced_submission
    assert {s["name"] for s in spans} == SUBMISSION_SPAN_NAMES


async def test_each_labelled_message_leads_with_the_strategy_name(capfire, tmp_path):
    await fixed_run(tmp_path, "tech-momentum")
    spans = capfire.exporter.exported_spans_as_dict()
    for name, rest in LABELLED.items():
        message = first(spans, name)["attributes"]["logfire.msg"]
        assert message.startswith(f"tech-momentum · {rest}"), (name, message)
    # The scored log is untouched: brief() matches its template, which is its name.
    (scored,) = [s for s in spans if s["name"] == "bazaar.run scored"]
    assert scored["attributes"]["logfire.msg_template"] == "bazaar.run scored"


def test_the_agents_spans_lead_with_the_strategy_name(traced_submission):  # noqa: F811
    _, spans, _ = traced_submission
    for name, rest in {
        "trading.decision": "day 1 · agent decision",
        "research.tool": "research tool",
        "runner.decision": "day 1 · decide",
        "runner.run": "run",
    }.items():
        message = first(spans, name)["attributes"]["logfire.msg"]
        assert message == f"news-reader · {rest}", (name, message)


@pytest.mark.parametrize("hostile", ["{evil} {0}", "{strategy} · {day}", "%s {experiment_id}"])
async def test_a_strategy_name_is_never_template_text(capfire, tmp_path, hostile):
    await fixed_run(tmp_path, hostile)
    spans = capfire.exporter.exported_spans_as_dict()
    for name in LABELLED:
        attributes = first(spans, name)["attributes"]
        assert attributes["logfire.msg"].startswith(f"{hostile} · "), name
        assert attributes["logfire.msg_template"].startswith("{strategy} · "), name
        assert hostile not in attributes["logfire.msg_template"], name
    assert {s["name"] for s in spans} == FIXED_RUN_SPAN_NAMES


async def test_without_a_strategy_name_the_message_still_renders(capfire, tmp_path):
    await fixed_run(tmp_path, None)  # record_run falls back to the policy_ref
    spans = capfire.exporter.exported_spans_as_dict()
    for name in LABELLED:
        assert first(spans, name)["attributes"]["logfire.msg"].startswith("scripted-test · ")
