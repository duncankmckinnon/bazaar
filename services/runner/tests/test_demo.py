import json
from decimal import Decimal
from uuid import UUID, uuid5

import httpx
import pytest
from bazaar_protocol import OrderRequest, OrderSide
from bazaar_runner import __main__ as cli
from bazaar_runner.demo import (
    AGENT_FIXTURE_REF,
    BUY_AND_HOLD_REF,
    CASH_ONLY_REF,
    DEMO_SYMBOLS,
    MOMENTUM_REF,
    Launch,
    ScriptedMomentum,
    demo_script,
    run_demo,
)
from bazaar_runner.http_market import RUNNER_TOKEN_ENV, RUNNER_TOKEN_HEADER, HttpMarketPort
from bazaar_runner.market import ApprovalDenied
from bazaar_runner.record import RunRecord
from pydantic import BaseModel

from .market_fakes import (
    SESSIONS,
    SPEC,
    TOKEN,
    InMemoryMarket,
    delegating_transport,
    refused_sentence,
)

MOMENTUM = Launch(MOMENTUM_REF, UUID(int=0xE1), UUID(int=0xA1))
CASH = Launch(CASH_ONLY_REF, UUID(int=0xE2), UUID(int=0xA2))
HOLD = Launch(BUY_AND_HOLD_REF, UUID(int=0xE3), UUID(int=0xA3))
REFUSED = Launch(MOMENTUM_REF, UUID(int=0xE4), UUID(int=0xA4))
DEMO = {"data_version": "synthetic-v1", "execution_rule_version": "exec-v1"}


class CountingMarket(InMemoryMarket):
    def __init__(self) -> None:
        super().__init__()
        self.price_reads = 0

    async def price_at(self, symbol, cutoff):
        self.price_reads += 1
        return await super().price_at(symbol, cutoff)


class RefusingMarket(InMemoryMarket):
    async def set_cutoff(self, *args):
        self.calls.append(("set_cutoff", args[1]))
        raise ApprovalDenied("This approval does not allow the call")


class Evaluated(BaseModel):
    status: str


async def cash_only(ctx, account):
    return ()


POLICIES = {
    MOMENTUM_REF: lambda prices: ScriptedMomentum(DEMO_SYMBOLS, prices),
    CASH_ONLY_REF: lambda prices: cash_only,
}


def test_demo_script_is_the_agreed_fortnight():
    assert demo_script() == SPEC.script
    assert [s.date.day for s in demo_script().sessions] == [2, 3, 4, 5, 6, 9, 10, 11, 12, 13]


async def test_demo_runs_share_a_schedule_and_the_refusal_is_recorded(tmp_path):
    ports = {MOMENTUM: CountingMarket(), CASH: CountingMarket(), REFUSED: RefusingMarket()}
    momentum, cash, refused = await run_demo(
        [MOMENTUM, CASH, REFUSED],
        ports,
        POLICIES,
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        evaluate=lambda record: Evaluated(status=record["status"]),
        **DEMO,
    )

    assert momentum.status == cash.status == "completed"
    assert {r.manifest.schedule_digest for r in (momentum, cash, refused)} == {
        momentum.manifest.schedule_digest
    }
    assert {(r.manifest.period_start, r.manifest.period_end) for r in (momentum, cash)} == {
        (SESSIONS[0].open_at, SESSIONS[-1].close_at)
    }
    assert {r.manifest.experiment_id for r in (momentum, cash, refused)} == {
        MOMENTUM.experiment_id,
        CASH.experiment_id,
        REFUSED.experiment_id,
    }
    # Fixture prices only rise: the momentum agent buys each symbol at the second open.
    assert {o.request.symbol for o in momentum.orders} == set(DEMO_SYMBOLS)
    assert {o.result.status for o in momentum.orders} == {"filled"}
    assert cash.orders == () and cash.final_account.cash == Decimal(10000)
    # Each policy reads only its own run's port.
    assert ports[MOMENTUM].price_reads > 0 and ports[CASH].price_reads == 0

    assert refused.status == "failed" and refused.failure_code == "approval_denied"
    assert ports[REFUSED].calls == [("set_cutoff", SESSIONS[0].open_at)]
    written = [RunRecord.model_validate_json(f.read_text()) for f in tmp_path.glob("*/record.json")]
    assert sorted(written, key=str) == sorted([momentum, cash, refused], key=str)
    assert refused.failure == refused_sentence(REFUSED.approval_id, REFUSED.experiment_id)
    # Every run is evaluated, the refused one included.
    assert len(list(tmp_path.glob("*/evaluation.json"))) == 3


async def test_a_failing_evaluator_does_not_stop_the_next_launch(tmp_path):
    def broken(record):
        raise RuntimeError("evals fell over")

    first, second = await run_demo(
        [MOMENTUM, CASH],
        {MOMENTUM: InMemoryMarket(), CASH: InMemoryMarket()},
        POLICIES,
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        evaluate=broken,
        **DEMO,
    )
    assert first.status == second.status == "completed"
    assert len(list(tmp_path.glob("*/record.json"))) == 2
    assert list(tmp_path.glob("*/evaluation.json")) == []


async def test_the_real_buy_and_hold_fills_its_whole_basket_at_its_sizing_prices(tmp_path):
    baselines = pytest.importorskip("bazaar_replay.baselines")
    market = InMemoryMarket()
    (record,) = await run_demo(
        [HOLD],
        {HOLD: market},
        {BUY_AND_HOLD_REF: lambda prices: baselines.BuyAndHold(DEMO_SYMBOLS, prices)},
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )
    assert record.status == "completed"
    assert [o.request.symbol for o in record.orders] == list(DEMO_SYMBOLS)
    for order in record.orders:
        assert order.result.status == "filled"
        sizing = await market.price_at(order.request.symbol, order.decided_at)
        assert order.result.unit_price == sizing.price


async def test_the_runner_token_never_reaches_spans_or_files(capfire, tmp_path):
    fake = InMemoryMarket()
    served = delegating_transport(fake)

    cutoffs = []

    async def leaky(request):
        # A transport failure that echoes the token, on the first decision's cutoff (control route).
        if request.url.path.endswith("/cutoff"):
            cutoffs.append(request)
            if len(cutoffs) == 3:
                token = request.headers[RUNNER_TOKEN_HEADER]
                raise httpx.ConnectError(f"refused, header was {token}")
        return await served.handle_async_request(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(leaky), base_url="http://m")
    port = HttpMarketPort(client, MOMENTUM.experiment_id, SPEC.approval_id, TOKEN)
    launch = Launch(MOMENTUM_REF, MOMENTUM.experiment_id, SPEC.approval_id)

    async def buy(ctx, account):
        if ctx.event_sequence:
            return ()
        order = OrderRequest(
            client_order_id=ctx.experiment_id, symbol="AAPL", side=OrderSide.BUY, quantity=1
        )
        return (order,)

    (record,) = await run_demo(
        [launch],
        {launch: port},
        {MOMENTUM_REF: lambda prices: buy},
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )
    assert record.status == "failed" and record.failure_code == "internal_error"

    spans = json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)
    assert "runner.decision" in spans and "exception" in spans
    assert TOKEN not in spans and "[redacted]" in spans
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text()


def cli_ids(*rows) -> list[str]:
    return [
        arg
        for n, (prefix, _, _) in enumerate(rows or cli.DEMO_LAUNCHES)
        for kind in cli.KINDS
        for arg in (f"--{prefix}-{kind}-id", str(UUID(int=0x100 * (n + 1) + len(kind))))
    ]


@pytest.fixture
def offline_cli(monkeypatch):
    """The CLI with telemetry off and policies that need no demo-only packages."""
    monkeypatch.setattr(cli, "configure_telemetry", lambda: None)
    monkeypatch.setattr(
        cli,
        "load_policies",
        lambda market_url: {ref: (lambda prices: cash_only) for _, ref, _ in cli.DEMO_LAUNCHES},
    )
    for prefix in [p for p, _, _ in cli.DEMO_LAUNCHES] + ["agent"]:
        for kind in cli.KINDS:
            monkeypatch.delenv(cli._env_name(prefix, kind), raising=False)
    return cli


def test_cli_stops_at_startup_without_the_token(offline_cli, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv(RUNNER_TOKEN_ENV, raising=False)
    code = cli.main(["--demo", "--runs-dir", str(tmp_path), *cli_ids()])
    assert code == 2
    assert RUNNER_TOKEN_ENV in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_cli_refuses_two_launches_in_one_experiment(offline_cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(RUNNER_TOKEN_ENV, TOKEN)
    ids = cli_ids()
    shared = ids[ids.index("--agent-fixture-experiment-id") + 1]
    ids[ids.index("--cash-only-experiment-id") + 1] = shared
    code = cli.main(["--demo", "--runs-dir", str(tmp_path), *ids])
    err = capsys.readouterr().err
    assert code == 2
    assert f"experiment_id {shared} is used by more than one launch" in err
    assert "agent-fixture-v1 and baseline-cash-only" in err
    assert TOKEN not in err and list(tmp_path.iterdir()) == []


def test_cli_refuses_a_shared_approval_too(offline_cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(RUNNER_TOKEN_ENV, TOKEN)
    ids = cli_ids()
    ids[ids.index("--momentum-approval-id") + 1] = ids[ids.index("--buy-and-hold-approval-id") + 1]
    assert cli.main(["--demo", "--runs-dir", str(tmp_path), *ids]) == 2
    assert "approval_id" in capsys.readouterr().err


async def test_run_demo_itself_refuses_a_shared_experiment(tmp_path):
    twin = Launch(CASH_ONLY_REF, MOMENTUM.experiment_id, UUID(int=0xAB))
    with pytest.raises(ValueError, match="more than one launch"):
        await run_demo(
            [MOMENTUM, twin],
            {MOMENTUM: InMemoryMarket(), twin: InMemoryMarket()},
            POLICIES,
            starting_cash=Decimal(10000),
            runs_dir=tmp_path,
            **DEMO,
        )
    assert list(tmp_path.iterdir()) == []


def test_cli_launches_four_runs_unless_momentum_is_dropped(offline_cli):
    args = cli.parse_args(["--demo", *cli_ids()])
    assert args.data_version == "alpaca-bars-v1"
    assert [ref for _, ref, _ in cli.demo_launches(args)] == [
        "agent-fixture-v1",
        "scripted-momentum-v1",
        "baseline-buy-and-hold",
        "baseline-cash-only",
    ]
    rows = [r for r in cli.DEMO_LAUNCHES if r[1] != MOMENTUM_REF]
    args = cli.parse_args(["--demo", "--no-momentum", *cli_ids(*rows)])
    assert MOMENTUM_REF not in [ref for _, ref, _ in cli.demo_launches(args)]


def test_the_old_agent_flags_still_name_the_momentum_run(offline_cli, monkeypatch):
    rows = [r for r in cli.DEMO_LAUNCHES if r[1] != MOMENTUM_REF]
    by_flag = cli.parse_args(
        [
            "--demo",
            *cli_ids(*rows),
            "--agent-experiment-id",
            str(UUID(int=0xBEEF)),
            "--agent-approval-id",
            str(UUID(int=0xBEF0)),
        ]
    )
    assert (by_flag.momentum_experiment_id, by_flag.momentum_approval_id) == (
        UUID(int=0xBEEF),
        UUID(int=0xBEF0),
    )
    monkeypatch.setenv("BAZAAR_AGENT_EXPERIMENT_ID", str(UUID(int=0xCAFE)))
    monkeypatch.setenv("BAZAAR_AGENT_APPROVAL_ID", str(UUID(int=0xCAFF)))
    by_env = cli.parse_args(["--demo", *cli_ids(*rows)])
    assert (by_env.momentum_experiment_id, by_env.momentum_approval_id) == (
        UUID(int=0xCAFE),
        UUID(int=0xCAFF),
    )


def test_cli_requires_every_launch_id(offline_cli, capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--demo"])
    err = capsys.readouterr().err
    assert "--agent-fixture-experiment-id" in err and "--cash-only-approval-id" in err


AGENT = Launch(AGENT_FIXTURE_REF, UUID(int=0xE5), UUID(int=0xA5))


async def test_demo_agent_launch_places_its_own_order_beside_the_baselines(capfire, tmp_path):
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import (
        AGENT_FIXTURE_INSTRUCTIONS,
        fixture_model_factory,
        make_agent_decider,
    )
    from bazaar_runner.agent_step import AgentStep

    agent_market = InMemoryMarket()
    ports = {AGENT: agent_market, MOMENTUM: InMemoryMarket(), CASH: InMemoryMarket()}
    policies = POLICIES | {
        AGENT_FIXTURE_REF: lambda prices: AgentStep(
            make_agent_decider(AGENT_FIXTURE_INSTRUCTIONS, fixture_model_factory()),
            market_url="http://market",
            transport=delegating_transport(agent_market, approval_id=AGENT.approval_id),
        )
    }
    agent, momentum, cash = await run_demo(
        [AGENT, MOMENTUM, CASH],
        ports,
        policies,
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )

    assert agent.status == momentum.status == cash.status == "completed", agent.failure
    (order,) = agent.orders
    assert order.event_sequence == 0 and order.result.status == "filled"
    assert (order.request.symbol, order.request.quantity) == ("AAPL", 10)
    assert order.result.client_order_id == uuid5(AGENT.experiment_id, "decision:0")
    assert agent.decision_errors == ()
    assert [c[0] for c in agent_market.calls].count("submit") == 1
    assert {r.manifest.schedule_digest for r in (agent, momentum, cash)} == {
        agent.manifest.schedule_digest
    }
    assert {o.request.symbol for o in momentum.orders} == set(DEMO_SYMBOLS)
    assert cash.orders == ()

    spans = capfire.exporter.exported_spans_as_dict()
    by_id = {s["context"]["span_id"]: s for s in spans}
    first = next(
        s
        for s in spans
        if s["name"] == "runner.decision"
        and s["attributes"].get("agent")
        and s["attributes"]["event_sequence"] == 0
    )
    assert first["attributes"]["client_order_id"] == str(order.result.client_order_id)
    assert first["attributes"]["reconcile"] == "not_needed"
    trading = [s for s in spans if s["name"] == "trading.decision"]
    assert len(trading) == len(SESSIONS)
    assert by_id[trading[0]["parent"]["span_id"]] is first
    assert TOKEN not in json.dumps(spans, default=str)
