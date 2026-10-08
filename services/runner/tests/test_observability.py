"""Submissions trace the agent with content; every span carries bazaar.*; no secret leaks."""

import json

import httpx
import logfire
import pytest
from bazaar_protocol import telemetry as protocol_telemetry
from bazaar_protocol.telemetry import scrubbing_options
from bazaar_runner import submission
from bazaar_runner.agent import fixture_model_factory
from bazaar_runner.submission import SubmissionFailed, run_submission, submission_experiment_id
from logfire._internal.integrations.httpx import make_async_request_hook, make_async_response_hook
from logfire.testing import TestExporter
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic_ai.messages import ModelResponse, ToolCallPart
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


# HTTP spans: a process that instruments httpx records one per runner market request (the agent
# harness suppresses its own). These runs wrap the market transport in OTel's httpx
# instrumentation (logfire.instrument_httpx() never patches the MockTransport used offline) and
# configure Logfire with the shared production scrubbing.
AUTHORIZATION = "Bearer sentinel-authorization-6e7f"
HEADER_KEYS = ("http.request.header.", "http.response.header.")


class WithHeaders(httpx.AsyncBaseTransport):
    """A client that also carries the admin token and an Authorization header (hostile case)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, headers: dict[str, str]) -> None:
        self.inner = inner
        self.headers = headers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers.update(self.headers)
        return await self.inner.handle_async_request(request)


def http_traced_submission(monkeypatch, tmp_path, *, capture_headers: bool):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=scrubbing_options(),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    market = GrantingMarket()
    hooks = {}
    if capture_headers:
        # Logfire's own header capture, as instrument_httpx(capture_headers=True) installs it.
        hooks = {
            "request_hook": make_async_request_hook(None, True, False),
            "response_hook": make_async_response_hook(
                None, True, False, logfire.DEFAULT_LOGFIRE_INSTANCE
            ),
        }
    transport = AsyncOpenTelemetryTransport(market.transport(), **hooks)
    if capture_headers:
        transport = WithHeaders(
            transport,
            {
                "X-Bazaar-Admin-Token": SENTINELS["BAZAAR_ADMIN_TOKEN"],
                "Authorization": AUTHORIZATION,
            },
        )
    monkeypatch.setattr(submission, "_transport_for", lambda url: transport)
    monkeypatch.setattr(submission, "configure_telemetry", lambda: None)
    monkeypatch.setattr(submission, "_model_factory", lambda model: fixture_model_factory())
    run_submission(
        submission_id="obs-http",
        name="http",
        instructions=INSTRUCTIONS,
        market_url="http://market",
        runner_token=TOKEN,
        runs_dir=tmp_path,
    )
    (approval,) = market.grants
    return exporter.exported_spans_as_dict(), approval


def http_spans(spans):
    return [
        s
        for s in spans
        if "http.request.method" in s["attributes"] or "http.method" in s["attributes"]
    ]


def header_attributes(spans) -> dict[str, list[str]]:
    by_key: dict[str, list[str]] = {}
    for span in spans:
        for key, value in span["attributes"].items():
            if key.startswith(HEADER_KEYS):
                by_key.setdefault(key, []).append(str(value))
    return by_key


def assert_no_secret(spans):
    exported = json.dumps(spans, default=str)
    for secret in (*SENTINELS.values(), TOKEN, AUTHORIZATION, AUTHORIZATION.split()[1]):
        assert secret not in exported


def test_http_spans_carry_no_header_values(monkeypatch, tmp_path):
    spans, approval = http_traced_submission(monkeypatch, tmp_path, capture_headers=False)
    assert http_spans(spans), "the market requests are traced"
    assert any(s["attributes"].get("http.url", "").endswith("/control/grants") for s in spans)
    assert header_attributes(spans) == {}
    # The approval id is not a secret (the evaluator's span carries approval_id), but no
    # header-named attribute carries it.
    names = ("header", "x-bazaar")
    assert not [
        (s["name"], k)
        for s in spans
        for k, v in s["attributes"].items()
        if any(n in k.lower() for n in names) and approval in str(v)
    ]
    assert_no_secret(spans)
    # The production patterns leave token usage alone.
    assert any(isinstance(s["attributes"].get("gen_ai.usage.input_tokens"), int) for s in spans)


def test_captured_headers_have_their_secrets_scrubbed(monkeypatch, tmp_path):
    """Hostile case: an httpx instrumentation with capture_headers=True, and requests that also
    carry the admin token and an Authorization header."""
    spans, approval = http_traced_submission(monkeypatch, tmp_path, capture_headers=True)
    by_key = header_attributes(spans)
    # Capture is on (the header attributes exist), and every capability or secret header is
    # scrubbed, the approval header included.
    for key in (
        "http.request.header.x-bazaar-approval",
        "http.request.header.x-bazaar-runner-token",
        "http.request.header.x-bazaar-admin-token",
        "http.request.header.authorization",
    ):
        assert by_key[key] and all("[Scrubbed due to" in v for v in by_key[key]), key
    assert not [v for values in by_key.values() for v in values if approval in v]
    assert_no_secret(spans)
    assert any(isinstance(s["attributes"].get("gen_ai.usage.input_tokens"), int) for s in spans)
    # The strategy attributes survive the extra patterns.
    (run,) = named(spans, "runner.run")
    assert run["attributes"]["bazaar.experiment_id"] == str(submission_experiment_id("obs-http"))
    assert {run["attributes"][k] for k in ("bazaar.strategy_name", "bazaar.policy_kind")} == {
        "http",
        "agent",
    }
    # The runner token header goes on the control routes only (grants, cutoff, accounts, close).
    token_routes = {
        httpx.URL(s["attributes"]["http.url"]).path.rsplit("/", 1)[-1]
        for s in spans
        if "http.request.header.x-bazaar-runner-token" in s["attributes"]
    }
    assert token_routes and token_routes <= {"grants", "cutoff", "accounts", "close"}


def test_a_process_configured_by_the_web_keeps_its_service_name(monkeypatch, tmp_path):
    """The web app configures Logfire first; run_submission must not take the process over."""
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        service_name="bazaar-web",
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    configured = []
    real_configure = protocol_telemetry.configure

    def spy(service_name, service_version=None, **kwargs):
        configured.append(
            (service_name, kwargs, real_configure(service_name, service_version, **kwargs))
        )
        return configured[-1][2]

    monkeypatch.setattr(protocol_telemetry, "configure", spy)
    market = GrantingMarket()
    transport = market.transport()
    monkeypatch.setattr(submission, "_transport_for", lambda url: transport)
    monkeypatch.setattr(submission, "_model_factory", lambda model: fixture_model_factory())
    # configure_telemetry is not replaced: run_submission really asks to configure.
    run_submission(
        submission_id="web-1",
        name="web",
        instructions=INSTRUCTIONS,
        market_url="http://market",
        runner_token=TOKEN,
        runs_dir=tmp_path,
    )
    assert configured == [("bazaar-runner", {"managed_variables": True}, False)]
    spans = exporter.exported_spans_as_dict(include_resources=True)
    assert named(spans, "runner.run")
    assert {s["resource"]["attributes"]["service.name"] for s in spans} == {"bazaar-web"}


# Exception text is never scrubbed by Logfire (exception.message and exception.stacktrace are
# safe keys), so a secret raised inside an error must be redacted before any span records it.
EXCEPTION_SECRETS = (*SENTINELS.values(), TOKEN)
HISTORY_END = "2026-02-01T00:00:00Z"


@pytest.fixture
def secret_env(monkeypatch, capfire):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BAZAAR_RUNNER_TOKEN", TOKEN)
    monkeypatch.setattr(protocol_telemetry, "_secrets", None)  # read the env above, once
    monkeypatch.setattr(submission, "configure_telemetry", lambda: None)
    return capfire


def leaky_message() -> str:
    return f"upstream said key={SENTINELS['PYDANTIC_AI_GATEWAY_API_KEY']} token {TOKEN}"


def exception_messages(spans) -> list[str]:
    return [
        str(event["attributes"].get("exception.message"))
        for span in spans
        for event in span.get("events", [])
        if event["name"] == "exception"
    ]


def assert_nowhere(spans, *texts: str) -> None:
    # A bare `assert secret not in <huge string>` makes pytest diff the whole export for minutes.
    exported = json.dumps(spans, default=str)
    leaked = [s for s in EXCEPTION_SECRETS if any(s in t for t in (exported, *texts))]
    if leaked:
        pytest.fail(f"secret sentinels leaked: {leaked}")


def test_a_model_and_market_client_raising_secrets_leave_no_secret(
    secret_env, monkeypatch, tmp_path
):
    """The agent's market read and then its model raise with the Gateway key and runner token."""
    market = GrantingMarket()
    inner = market.transport()

    class LeakyReads(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/history"):
                raise httpx.ConnectError(leaky_message())
            return await inner.handle_async_request(request)

    leaky = LeakyReads()
    monkeypatch.setattr(submission, "_transport_for", lambda url: leaky)

    def raising_factory(model):
        def build(model_ref):
            calls = []

            def call(messages, info):
                calls.append(messages)
                if len(calls) == 1:
                    args = {"request": {"start_at": "2026-01-01T00:00:00Z", "end_at": HISTORY_END}}
                    return ModelResponse([ToolCallPart("account_history", args)])
                raise RuntimeError(leaky_message())

            return FunctionModel(call)

        return build

    monkeypatch.setattr(submission, "_model_factory", raising_factory)
    run_dir = run_submission(
        submission_id="leak-1",
        name="leaky",
        instructions=INSTRUCTIONS,
        market_url="http://market",
        runner_token=TOKEN,
        runs_dir=tmp_path,
    )
    spans = secret_env.exporter.exported_spans_as_dict()
    messages = exception_messages(spans)
    # The model's error was recorded on the agent's spans, redacted.
    assert any(m.startswith("RuntimeError: upstream said key=[REDACTED]") for m in messages)
    assert any(s["name"].startswith("chat ") and s.get("events") for s in spans)
    assert_nowhere(spans, *(p.read_text() for p in run_dir.rglob("*") if p.is_file()))


def test_a_runner_market_error_holding_secrets_leaves_no_secret(secret_env, monkeypatch, tmp_path):
    """The runner's own control call fails with the key and token in the error."""
    market = GrantingMarket()
    inner = market.transport()
    cutoffs = []

    class LeakyCutoff(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/cutoff"):
                cutoffs.append(request)
                if len(cutoffs) == 3:
                    raise httpx.ConnectError(leaky_message())
            return await inner.handle_async_request(request)

    leaky = LeakyCutoff()
    monkeypatch.setattr(submission, "_transport_for", lambda url: leaky)
    monkeypatch.setattr(submission, "_model_factory", lambda model: fixture_model_factory())
    with pytest.raises(SubmissionFailed) as failed:
        run_submission(
            submission_id="leak-2",
            name="leaky",
            instructions=INSTRUCTIONS,
            market_url="http://market",
            runner_token=TOKEN,
            runs_dir=tmp_path,
        )
    spans = secret_env.exporter.exported_spans_as_dict()
    assert "[REDACTED]" in str(failed.value)
    # The third cutoff is the first mark's: that span records the redacted market error.
    (mark,) = [s for s in named(spans, "runner.mark") if s.get("events")]
    assert "[REDACTED]" in json.dumps(mark["events"], default=str)
    assert str(failed.value).startswith("the run failed: during the mark at")
    assert_nowhere(spans, str(failed.value))


def judged_submission(monkeypatch, tmp_path, grade):
    """A submission with the online strategy judge on, graded by a local function model."""
    from bazaar_agent import strategy_evaluation

    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.setattr(strategy_evaluation, "judge_model", lambda: FunctionModel(grade))
    market = GrantingMarket()
    transport = market.transport()
    monkeypatch.setattr(submission, "_transport_for", lambda url: transport)
    monkeypatch.setattr(submission, "configure_telemetry", lambda: None)
    monkeypatch.setattr(submission, "_model_factory", lambda model: fixture_model_factory())
    return run_submission(
        submission_id="judged-1",
        name="judged",
        instructions=INSTRUCTIONS,
        market_url="http://market",
        runner_token=TOKEN,
        runs_dir=tmp_path,
    )


def evaluation_logs(capfire):
    return [
        log
        for log in capfire.log_exporter.exported_logs_as_dicts()
        if "gen_ai.evaluation.name" in log["attributes"]
        or "StrategyAdherence" in str(log.get("body"))
    ]


def test_the_strategy_judge_spans_and_events_carry_the_bazaar_attributes(
    monkeypatch, capfire, tmp_path
):
    def grade(messages, info):
        verdict = {"pass": True, "score": 1.0, "reason": "Followed the strategy."}
        return ModelResponse([ToolCallPart(info.output_tools[0].name, verdict)])

    judged_submission(monkeypatch, tmp_path, grade)
    spans = capfire.exporter.exported_spans_as_dict()
    judged = [s for s in spans if s["name"].startswith(("trading.decision.evaluated", "evaluator"))]
    assert judged
    expected = {"bazaar.submission_id": "judged-1", "bazaar.strategy_name": "judged"}
    for span in judged:
        assert {k: span["attributes"].get(k) for k in expected} == expected, span["name"]
    events = evaluation_logs(capfire)
    assert len(events) >= 10
    for event in events:
        assert {k: event["attributes"].get(k) for k in expected} == expected


def test_a_judge_failure_holding_secrets_leaves_no_secret(secret_env, monkeypatch, tmp_path):
    def grade(messages, info):
        raise RuntimeError(leaky_message())

    run_dir = judged_submission(monkeypatch, tmp_path, grade)
    spans = secret_env.exporter.exported_spans_as_dict()
    events = evaluation_logs(secret_env)
    assert any("[REDACTED]" in json.dumps(event, default=str) for event in events)
    # The error was raised by the judge's model, inside its traced chat span: that span recorded
    # it (message and stacktrace) already redacted.
    judge_chats = [
        s for s in spans if s["attributes"].get("gen_ai.request.model") == "function:grade:"
    ]
    assert judge_chats
    for chat in judge_chats:
        (error,) = [e for e in chat.get("events", []) if e["name"] == "exception"]
        assert error["attributes"]["exception.type"].endswith("RedactedError")
        assert "[REDACTED]" in error["attributes"]["exception.message"]
        assert "exception.stacktrace" in error["attributes"]
    files = [p.read_text() for p in run_dir.rglob("*") if p.is_file()]
    assert_nowhere(spans, json.dumps(events, default=str), *files)


JUDGE_REASON = "Bought ten AAPL at the first open and held, as instructed."


def grade(messages, info):
    verdict = {"pass": True, "score": 0.75, "reason": JUDGE_REASON}
    return ModelResponse([ToolCallPart(info.output_tools[0].name, verdict)])


@pytest.fixture
def judged_run(monkeypatch, capfire, tmp_path):
    """A submission with the online strategy judge on, graded by a local function model, under
    sentinel secrets. run_submission wraps the run in strategy_evaluation_session."""
    from bazaar_agent import strategy_evaluation

    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "1")
    monkeypatch.setattr(strategy_evaluation, "judge_model", lambda: FunctionModel(grade))
    market = GrantingMarket()
    transport = market.transport()
    monkeypatch.setattr(submission, "_transport_for", lambda url: transport)
    monkeypatch.setattr(submission, "configure_telemetry", lambda: None)
    monkeypatch.setattr(submission, "_model_factory", lambda model: fixture_model_factory())
    run_submission(
        submission_id="judged-1",
        name="judged",
        instructions=INSTRUCTIONS,
        market_url="http://market",
        runner_token=TOKEN,
        runs_dir=tmp_path,
    )
    # run_submission returns only after the session drained every judge.
    return capfire.exporter.exported_spans_as_dict(), capfire.log_exporter.exported_logs_as_dicts()


def test_adherence_results_reach_logfire_as_evaluation_events(judged_run):
    """The runner session's completion signal runs beside the SDK's OTel events, not instead."""
    _, logs = judged_run
    events = [log["attributes"] for log in logs if "gen_ai.evaluation.name" in log["attributes"]]
    scores = [e for e in events if e["gen_ai.evaluation.name"] == "strategy_adherence"]
    passes = [e for e in events if e["gen_ai.evaluation.name"] == "strategy_adherence_pass"]
    assert len(scores) == len(passes) == 10
    for event in scores:
        assert event["gen_ai.evaluation.score.value"] == 0.75
        assert event["gen_ai.evaluation.explanation"] == JUDGE_REASON
    for event in passes:
        assert event["gen_ai.evaluation.score.label"] == "pass"
        assert event["gen_ai.evaluation.explanation"] == JUDGE_REASON
    for event in events:
        assert event["gen_ai.evaluation.target"] == "trading.decision"
        assert event["bazaar.submission_id"] == "judged-1"
        assert event["bazaar.strategy_name"] == "judged"


def ancestors(span, by_id):
    while span.get("parent"):
        span = by_id.get(span["parent"]["span_id"])
        if span is None:
            return
        yield span


def test_the_judge_model_requests_are_traced_under_the_evaluator(judged_run):
    spans, logs = judged_run
    by_id = {s["context"]["span_id"]: s for s in spans}
    judge_chats = [
        s for s in spans if s["attributes"].get("gen_ai.request.model") == "function:grade:"
    ]
    assert len(judge_chats) == 10
    for chat in judge_chats:
        assert chat["name"].startswith("chat ")
        assert any(a["name"].startswith("evaluator") for a in ancestors(chat, by_id))
        assert "gen_ai.usage.input_tokens" in chat["attributes"]
        # Content is on, as for the trader: the judge's prompt and verdict are visible.
        assert "gen_ai.input.messages" in chat["attributes"]
        assert JUDGE_REASON in str(chat["attributes"].get("gen_ai.output.messages"))
        assert chat["attributes"]["bazaar.submission_id"] == "judged-1"
    # The judge's prompt holds the decision evidence, never a credential.
    exported = json.dumps([spans, logs], default=str)
    for secret in (*SENTINELS.values(), TOKEN):
        assert secret not in exported


def test_the_baggage_allow_list_is_exactly_the_strategy_attributes():
    """bazaar_attributes() becomes OTel baggage, which is sent to the Gateway (and the market) as
    a header. Adding a key, such as the instructions or an IP hash, must fail this test."""
    from bazaar_runner.demo import Launch, demo_spec
    from bazaar_runner.record import bazaar_attributes

    spec = demo_spec(
        Launch("submission:x", submission_experiment_id("b-1"), submission_experiment_id("a")),
        data_version="demo-bundle-v1",
        execution_rule_version="exec-v1",
        starting_cash=10000,
    )
    full = bazaar_attributes(spec, "submission:x", "momo", "b-1", "@attendee")
    assert set(full) == {
        "bazaar.strategy_name",
        "bazaar.experiment_id",
        "bazaar.run_id",
        "bazaar.policy_kind",
        "bazaar.submission_id",
        "bazaar.handle",
    }
    assert all(isinstance(value, str) for value in full.values())
    # A demo launch has no submission or handle; nothing else is ever added.
    assert set(bazaar_attributes(spec, "baseline-cash-only", None, None, None)) == {
        "bazaar.strategy_name",
        "bazaar.experiment_id",
        "bazaar.run_id",
        "bazaar.policy_kind",
    }
    assert INSTRUCTIONS not in "".join(full.values())
