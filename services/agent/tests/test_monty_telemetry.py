"""Code-mode decisions trace Monty's own session and run spans; other decisions install nothing."""

import json

from bazaar_agent import trading
from bazaar_agent.trading import RuntimeConfig

from .test_trading import code_call, invoke, output, script

SENTINELS = {
    "PYDANTIC_AI_GATEWAY_API_KEY": "sentinel-gateway-key-0f1e",
    "BAZAAR_RUNNER_TOKEN": "sentinel-runner-token-7c6d",
    "LOGFIRE_TOKEN": "sentinel-logfire-token-4b5a",
}
MONTY_SPANS = ("session ", "run code")


def ancestors(span, by_id):
    while span.get("parent"):
        span = by_id.get(span["parent"]["span_id"])
        if span is None:
            return
        yield span


async def test_a_code_mode_decision_traces_monty_under_the_decision(monkeypatch, capfire):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    model, _ = script([code_call("x = 1 + 2\nx * 10")], lambda info: [output(info)])
    result, _ = await invoke(
        model, overrides={"runtime": RuntimeConfig(code_mode=True, instrument=True)}
    )
    assert result.error is None
    spans = capfire.exporter.exported_spans_as_dict()
    by_id = {s["context"]["span_id"]: s for s in spans}
    (decision,) = [s for s in spans if s["name"] == "trading.decision"]
    (session,) = [s for s in spans if s["name"].startswith("session ")]
    (run,) = [s for s in spans if s["name"] == "run code"]
    for span in (session, run):
        assert span["context"]["trace_id"] == decision["context"]["trace_id"]
        assert decision in ancestors(span, by_id)
    assert "x * 10" in run["attributes"]["code"]
    exported = json.dumps(spans, default=str)
    leaked = [value for value in SENTINELS.values() if value in exported]
    assert leaked == []


async def test_a_decision_without_code_mode_installs_no_monty_telemetry(monkeypatch, capfire):
    installs = []
    monkeypatch.setattr(trading, "instrument_monty", lambda: installs.append(True))
    model, _ = script(lambda info: [output(info)])
    result, _ = await invoke(model, overrides={"runtime": RuntimeConfig(instrument=True)})
    assert result.error is None and installs == []
    names = [s["name"] for s in capfire.exporter.exported_spans_as_dict()]
    assert not [n for n in names if n.startswith(MONTY_SPANS)]


def test_installing_twice_is_harmless(monkeypatch):
    import pydantic_monty
    from bazaar_agent import monty_telemetry

    calls = []

    def install(*, tracer):
        calls.append(tracer)
        raise RuntimeError("Monty telemetry is already configured")

    monkeypatch.setattr(pydantic_monty, "instrument_telemetry", install)
    monkeypatch.setattr(monty_telemetry, "_installed", False)
    monty_telemetry.instrument_monty()
    monty_telemetry.instrument_monty()
    assert len(calls) == 1
