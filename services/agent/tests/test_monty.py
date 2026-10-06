import asyncio
import json
from datetime import datetime

import pytest
from bazaar_agent.monty import CalculationSnapshot, MontyCalculator
from bazaar_protocol import PriceHistory
from pydantic import ValidationError
from pydantic_ai.messages import ToolCallPart

from .test_research import FUTURE, context, prices
from .test_trading import invoke, output, script


def calculator():
    snapshot = CalculationSnapshot(
        context=context().experiment, prices=(PriceHistory.model_validate(prices()),)
    )
    return MontyCalculator(snapshot)


async def test_trusted_market_binding_preserves_prices_and_rejects_injected_cash():
    from bazaar_protocol import AccountSnapshot, PortfolioSnapshot

    from .test_research import account, portfolio

    market_account = AccountSnapshot.model_validate(account())
    market_portfolio = PortfolioSnapshot.model_validate(portfolio())
    calc = calculator()
    calc.bind_market_state(market_account, market_portfolio)
    assert calc.snapshot.account == market_account
    assert calc.snapshot.portfolio == market_portfolio
    assert len(calc.snapshot.prices) == 1
    assert (
        json.loads((await calc.monty_inputs()).data)["portfolio"]["portfolio_value"]
        == portfolio()["portfolio_value"]
    )
    injected = MontyCalculator(
        CalculationSnapshot(
            context=context().experiment,
            account=market_account.model_copy(update={"cash": market_account.cash + 1}),
        )
    )
    with pytest.raises(ValueError, match="disagree"):
        injected.bind_market_state(market_account, market_portfolio)
    assert calc.reserve()
    with pytest.raises(ValueError, match="fresh"):
        calc.bind_market_state(market_account, market_portfolio)


async def test_actual_sdk_calculation_and_audit():
    calc = calculator()
    result = await calc.monty_calculate(
        "sum(float(p['price']) for p in inputs['prices'][0]['observations'])"
    )
    assert result.error is None
    assert json.loads(result.data.output_json) > 0
    assert result.data.code_digest and result.data.snapshot_digest
    assert result.data.sdk_version == "0.0.14"
    assert calc.records == [result.data]
    assert json.loads((await calc.monty_inputs()).data)["context"]["simulated_at"]


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.getenv('LOGFIRE_TOKEN')",
        "open('/etc/passwd').read()",
        "import datetime\ndatetime.datetime.now()",
        "requests.get('https://example.invalid')",
        "unknown_host_function()",
        "import subprocess\nsubprocess.run(['echo', 'unsafe'])",
    ],
)
async def test_host_capabilities_are_denied(code):
    calc = calculator()
    result = await calc.monty_calculate(code)
    assert result.error is not None
    assert calc.records[-1].status in ("denied", "runtime")
    assert calc.records[-1].output_json is None


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("1 +", "syntax"),
        ("1 / 0", "runtime"),
        ("float('inf')", "serialization"),
    ],
)
async def test_invalid_runtime_and_resource_results(code, status):
    calc = calculator()
    result = await calc.monty_calculate(code)
    assert result.error is not None
    assert calc.records[-1].status == status


async def test_no_artificial_code_or_output_limits_and_snapshot_cutoff():
    calc = calculator()
    result = await calc.monty_calculate("#" + "x" * 17000 + "\n'x' * 300000")
    assert result.error is None
    assert len(json.loads(result.data.output_json)) == 300000
    history = PriceHistory.model_validate(prices())
    unsafe = history.model_copy(update={"cutoff_at": datetime.fromisoformat(FUTURE)})
    with pytest.raises(ValidationError):
        CalculationSnapshot(context=context().experiment, prices=(unsafe,))
    with pytest.raises(ValidationError):
        CalculationSnapshot(context=context().experiment, prices=(history, history))


@pytest.mark.parametrize("during_spawn", [False, True])
async def test_cancellation_reaps_process(monkeypatch, during_spawn):
    processes = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    calc = calculator()
    task = asyncio.create_task(calc.monty_calculate("while True:\n pass"))
    if during_spawn:
        await asyncio.sleep(0)
    else:
        async with asyncio.timeout(5):
            while not processes:
                await asyncio.sleep(0.001)
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(process.returncode is not None for process in processes)


async def test_repeated_cancel_with_full_output_pipe_completes(monkeypatch):
    import sys

    original = asyncio.create_subprocess_exec
    gate = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('x'*2000000); sys.stdout.flush(); time.sleep(30)",
            **kwargs,
        )
        processes.append(process)
        communicate = process.communicate

        async def delayed(payload):
            await gate.wait()
            return await communicate(payload)

        process.communicate = delayed
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    calc = calculator()
    task = asyncio.create_task(calc.monty_calculate("1+2"))
    async with asyncio.timeout(5):
        while not processes or processes[0].stdout._transport.is_reading():
            await asyncio.sleep(0.001)
    task.cancel("PRIVATE-CANCEL-MESSAGE")
    await asyncio.sleep(0)
    task.cancel("PRIVATE-CANCEL-MESSAGE")
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert processes[0].returncode is not None
    assert calc.records[-1].status == "cancelled"


async def test_model_tool_integration_and_overall_budget():
    model, calls = script(
        [ToolCallPart("monty_calculate", {"code": "1+2"})], lambda info: [output(info)]
    )
    result, requests = await invoke(
        model, tools=("monty",), harness="monty", overrides={"calculator": calculator()}
    )
    assert result.error is None and result.decision.action == "hold"
    assert len(result.calculations) == 1
    assert json.loads(result.calculations[0].output_json) == 3
    assert len(calls) == 2 and not requests
    model, _ = script([ToolCallPart("monty_calculate", {"code": "1+2"})])
    from bazaar_agent.trading import DecisionBudget

    result, _ = await invoke(
        model,
        tools=("monty",),
        overrides={
            "calculator": calculator(),
            "budget": DecisionBudget(tool_calls=1, model_requests=1),
        },
    )
    assert result.error is not None and len(result.calculations) == 1


async def test_forecast_volatility_and_hypothetical_backtest():
    data = prices()
    point = data["observations"][0]
    data["observations"] = [
        dict(point, price=price, observed_at=date, available_at=date)
        for price, date in (
            ("10", "2020-03-30T12:00:00Z"),
            ("11", "2020-03-31T12:00:00Z"),
            ("12.34", "2020-04-01T12:00:00Z"),
        )
    ]
    calc = MontyCalculator(
        CalculationSnapshot(
            context=context().experiment, prices=(PriceHistory.model_validate(data),)
        )
    )
    result = await calc.monty_calculate("""import math
p = [float(x['price']) for x in inputs['prices'][0]['observations']]
r = [p[i] / p[i-1] - 1 for i in range(1, len(p))]
mean = sum(r)/len(r)
{'volatility': math.sqrt(sum((x-mean)**2 for x in r)/len(r)), 'forecast': p[-1]*(1+mean), 'buy_and_hold_return': p[-1]/p[0]-1}
""")
    assert result.error is None
    output = json.loads(result.data.output_json)
    assert output["buy_and_hold_return"] == pytest.approx(0.234)
    assert output["volatility"] > 0
    assert output["forecast"] > 12.34


async def test_prints_and_failure_diagnostics_are_not_truncated():
    calc = calculator()
    result = await calc.monty_calculate("print('x' * 6000)\nraise ValueError('diagnostic' * 1000)")
    assert result.error is not None
    record = calc.records[-1]
    assert "x" * 6000 in record.prints_json
    assert "diagnostic" * 1000 in record.error_text


async def test_spawn_failure_is_a_recorded_failure(monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("worker unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    calc = calculator()
    result = await calc.monty_calculate("1+2")
    assert result.error is not None
    assert calc.records[-1].status == "worker_error"
    assert "worker unavailable" in calc.records[-1].error_text


async def test_private_code_inputs_results_excluded_from_logfire(capfire):
    marker = "PRIVATE-MONTY-CODE-12345"
    calc = calculator()
    result = await calc.monty_calculate(repr(marker))
    assert json.loads(result.data.output_json) == marker
    assert marker in result.data.code
    spans = capfire.exporter.exported_spans_as_dict()
    assert marker not in json.dumps(spans, default=str)
    assert any(span["name"] == "monty.calculate" for span in spans)


@pytest.mark.parametrize(
    "code,status", [("1 +", "syntax"), ("1/0", "runtime"), ("float('inf')", "serialization")]
)
async def test_model_corrects_pure_calculation_failure(code, status):
    from pydantic_ai.messages import ToolReturnPart

    def correction(info):
        return [ToolCallPart("monty_calculate", {"code": "1+2"})]

    model, calls = script(
        [ToolCallPart("monty_calculate", {"code": code})],
        correction,
        lambda info: [output(info)],
    )
    calc = calculator()
    result, _ = await invoke(model, tools=("monty",), overrides={"calculator": calc})
    assert result.error is None and result.decision.action == "hold"
    assert [r.status for r in result.calculations] == [status, "ok"]
    feedback = [p for m in calls[1][0] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert feedback[0].content.data.error_text == calc.records[0].error_text
    assert feedback[0].content.data.code == code
    assert result.usage.tool_calls == 2 and result.usage.model_requests == 3


async def test_denied_host_boundary_stops_decision():
    model, calls = script([ToolCallPart("monty_calculate", {"code": "open('/etc/passwd')"})])
    result, _ = await invoke(model, tools=("monty",), overrides={"calculator": calculator()})
    assert result.error is not None and result.decision is None
    assert result.calculations[0].status == "denied" and len(calls) == 1


async def test_recovery_still_obeys_overall_tool_budget():
    from bazaar_agent.trading import DecisionBudget

    model, _ = script(
        [ToolCallPart("monty_calculate", {"code": "1/0"})],
        [ToolCallPart("monty_calculate", {"code": "1+2"})],
    )
    result, _ = await invoke(
        model,
        tools=("monty",),
        overrides={
            "calculator": calculator(),
            "budget": DecisionBudget(tool_calls=1),
        },
    )
    assert result.error.code == "conflict"
    assert len(result.calculations) == 1 and result.calculations[0].status == "runtime"


async def test_private_failure_feedback_not_in_global_sdk_telemetry(capfire, caplog):
    import httpx
    import logfire
    from bazaar_agent.trading import run_decision
    from pydantic_ai import Agent

    from .test_research import account, portfolio
    from .test_trading import initial_router, inputs

    marker = "PRIVATE-CALC-DIAGNOSTIC-12345"
    Agent.instrument_all(True)
    try:
        model, _ = script(
            [
                ToolCallPart(
                    "monty_calculate", {"code": f"print({marker!r})\nraise ValueError({marker!r})"}
                )
            ],
            [ToolCallPart("monty_calculate", {"code": "1+2"})],
            lambda info: [output(info)],
        )
        cash = "98765432.10"
        transport = initial_router(
            lambda request: httpx.Response(500),
            initial_account=account(cash=cash),
            initial_portfolio=portfolio(cash=cash, portfolio_value=cash, source=marker),
        )
        async with httpx.AsyncClient(
            base_url="https://market.invalid", transport=httpx.MockTransport(transport)
        ) as client:
            logfire.instrument_httpx(
                client,
                capture_all=False,
                capture_headers=False,
                capture_request_body=False,
                capture_response_body=False,
            )
            result = await run_decision(client=client, model_factory=lambda ref: model, **inputs())
        assert result.error is None and marker in result.calculations[0].error_text
        assert marker in result.calculations[0].prints_json
        telemetry = json.dumps(capfire.exporter.exported_spans_as_dict(), default=str) + caplog.text
        assert marker not in telemetry and cash not in telemetry
    finally:
        Agent.instrument_all(False)


async def test_invalid_worker_response_is_fatal_boundary(monkeypatch):
    import sys

    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        return await original(sys.executable, "-c", 'print(\'{"status":"future"}\')', **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    calc = calculator()
    result = await calc.monty_calculate("1+2")
    assert result.error is not None and result.data.status == "worker_error"
    assert result.data.output_json is None


async def test_builtin_default_calculator_uses_initial_api_state():
    model, calls = script(
        [ToolCallPart("monty_calculate", {"code": "inputs['portfolio']['portfolio_value']"})],
        lambda info: [output(info)],
    )
    result, requests = await invoke(model)
    assert result.error is None and result.decision.action == "hold"
    assert len(result.calculations) == 1 and len(calls) == 2 and not requests
    from .test_research import portfolio

    assert json.loads(result.calculations[0].output_json) == portfolio()["portfolio_value"]
    assert json.loads(result.calculations[0].inputs_json)["prices"] == []


async def test_injected_calculation_cash_fails_before_model():
    from bazaar_protocol import AccountSnapshot

    from .test_research import account

    calc = MontyCalculator(
        CalculationSnapshot(
            context=context().experiment,
            account=AccountSnapshot.model_validate(account(cash="99999999")),
        )
    )
    model, calls = script(lambda info: [output(info)])
    result, requests = await invoke(model, overrides={"calculator": calc})
    assert result.error.code == "invalid_request" and not calls and not requests
    assert not calc.records


async def test_fixed_snapshot_does_not_ingest_model_or_research_arrays():
    from .test_trading import query

    model, _ = script(
        [ToolCallPart("prices", query())],
        [ToolCallPart("monty_calculate", {"code": "len(inputs['prices'])"})],
        lambda info: [output(info)],
    )
    result, requests = await invoke(model, payload=prices())
    assert result.error is None and len(requests) == 1
    assert json.loads(result.calculations[0].output_json) == 0


async def test_calculator_context_failure_stops_before_model():
    from uuid import UUID

    calc = MontyCalculator(
        CalculationSnapshot(
            context=context().experiment.model_copy(update={"account_id": UUID(int=999)}),
        )
    )
    model, calls = script(lambda info: [output(info)])
    result, _ = await invoke(model, overrides={"calculator": calc})
    assert result.error.code == "invalid_request" and not calls and not calc.records


async def test_invalid_unicode_code_is_recorded_privately(capfire):
    calc = calculator()
    result = await calc.monty_calculate("'\ud800'")
    assert result.error is not None and len(calc.records) == 1
    assert calc.records[0].code == "'\ud800'"
