import json
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

import httpx
import pytest
from bazaar_protocol import OrderRequest, OrderSide
from bazaar_runner import __main__ as cli
from bazaar_runner.demo import (
    AGENT_FIXTURE_REF,
    BUY_AND_HOLD_REF,
    BUY_AND_HOLD_SPY_REF,
    CASH_ONLY_REF,
    MOMENTUM_REF,
    MOMENTUM_V2_REF,
    DuplicateLaunch,
    Launch,
    ScriptedMomentum,
    run_demo,
)
from bazaar_runner.http_market import RUNNER_TOKEN_ENV, RUNNER_TOKEN_HEADER, HttpMarketPort
from bazaar_runner.market import ApprovalDenied
from bazaar_runner.record import RunRecord
from pydantic import BaseModel

from .market_fakes import (
    BUNDLE,
    SESSIONS,
    SPEC,
    SYMBOLS,
    TOKEN,
    InMemoryMarket,
    delegating_transport,
    fixture_bundle,
    refused_sentence,
)

MOMENTUM = Launch(MOMENTUM_REF, UUID(int=0xE1), UUID(int=0xA1))
CASH = Launch(CASH_ONLY_REF, UUID(int=0xE2), UUID(int=0xA2))
HOLD = Launch(BUY_AND_HOLD_REF, UUID(int=0xE3), UUID(int=0xA3))
REFUSED = Launch(MOMENTUM_REF, UUID(int=0xE4), UUID(int=0xA4))
DEMO = {"bundle": BUNDLE, "execution_rule_version": "exec-v1"}

# ScriptedMomentum on the fixture prices: every demo symbol it can afford a share of.
MOMENTUM_BUYS = set(SYMBOLS) - {"META", "MSFT"}


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
    MOMENTUM_REF: lambda bundle, prices: ScriptedMomentum(bundle.symbols, prices),
    CASH_ONLY_REF: lambda bundle, prices: cash_only,
}


def test_the_fixture_bundle_gives_the_fixture_calendar():
    assert BUNDLE.script == SPEC.script
    assert [s.date.day for s in BUNDLE.sessions] == [2, 3, 4, 5, 6, 9, 10, 11, 12, 13]


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
    # Fixture prices only rise: at the second open momentum puts 30% of its remaining cash into
    # each symbol in turn; by META ($701) and MSFT ($401) that buys no whole share.
    assert {o.request.symbol for o in momentum.orders} == MOMENTUM_BUYS
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
        {BUY_AND_HOLD_REF: lambda bundle, prices: baselines.BuyAndHold(bundle.symbols, prices)},
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )
    assert record.status == "completed"
    assert [o.request.symbol for o in record.orders] == list(SYMBOLS)
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
        {MOMENTUM_REF: lambda bundle, prices: buy},
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )
    assert record.status == "failed" and record.failure_code == "internal_error"

    spans = json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)
    assert "runner.decision" in spans and "exception" in spans
    assert TOKEN not in spans and "[REDACTED]" in spans
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
        lambda market_url, **_: {
            ref: (lambda bundle, prices: cash_only) for _, ref, _ in cli.DEMO_LAUNCHES
        },
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
    with pytest.raises(DuplicateLaunch, match="more than one launch"):
        await run_demo(
            [MOMENTUM, twin],
            {MOMENTUM: InMemoryMarket(), twin: InMemoryMarket()},
            POLICIES,
            starting_cash=Decimal(10000),
            runs_dir=tmp_path,
            **DEMO,
        )
    assert list(tmp_path.iterdir()) == []


def test_other_value_errors_are_not_swallowed_by_the_cli(offline_cli, monkeypatch, tmp_path):
    monkeypatch.setenv(RUNNER_TOKEN_ENV, TOKEN)

    async def broken(args):
        raise ValueError("a bug, not an operator mistake")

    monkeypatch.setattr(cli, "amain", broken)
    with pytest.raises(ValueError, match="a bug"):
        cli.main(["--demo", "--runs-dir", str(tmp_path), *cli_ids()])


def test_cli_launches_four_runs_unless_momentum_is_dropped(offline_cli):
    args = cli.parse_args(["--demo", *cli_ids()])
    assert args.data_version == "demo-bundle-v2"
    # The S&P 500 launch set (spec v5): equal-weight and momentum v1 are gone.
    assert [ref for _, ref, _ in cli.demo_launches(args)] == [
        "agent-fixture-v1",
        "scripted-momentum-v2",
        "baseline-buy-and-hold-spy",
        "baseline-cash-only",
    ]
    rows = [r for r in cli.DEMO_LAUNCHES if r[1] != MOMENTUM_V2_REF]
    args = cli.parse_args(["--demo", "--no-momentum", *cli_ids(*rows)])
    assert MOMENTUM_V2_REF not in [ref for _, ref, _ in cli.demo_launches(args)]


def test_the_deprecated_agent_names_still_set_momentum_but_warn(offline_cli, monkeypatch, caplog):
    rows = [r for r in cli.DEMO_LAUNCHES if r[1] != MOMENTUM_V2_REF]
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
    assert "--agent-experiment-id is deprecated" in caplog.text
    assert "use --momentum-experiment-id" in caplog.text

    caplog.clear()
    monkeypatch.setenv("BAZAAR_AGENT_EXPERIMENT_ID", str(UUID(int=0xCAFE)))
    monkeypatch.setenv("BAZAAR_AGENT_APPROVAL_ID", str(UUID(int=0xCAFF)))
    by_env = cli.parse_args(["--demo", *cli_ids(*rows)])
    assert (by_env.momentum_experiment_id, by_env.momentum_approval_id) == (
        UUID(int=0xCAFE),
        UUID(int=0xCAFF),
    )
    assert "$BAZAAR_AGENT_EXPERIMENT_ID is deprecated" in caplog.text
    assert "set $BAZAAR_MOMENTUM_EXPERIMENT_ID" in caplog.text


def test_the_primary_momentum_names_do_not_warn(offline_cli, caplog):
    args = cli.parse_args(["--demo", *cli_ids()])
    assert args.momentum_experiment_id is not None
    assert "deprecated" not in caplog.text


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
        AGENT_FIXTURE_REF: lambda bundle, prices: AgentStep(
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
    calls = [c[0] for c in agent_market.calls]
    assert calls.count("submit") == 1
    # It read AAPL news once, at the first decision, before it ordered.
    assert [c[:2] for c in agent_market.calls if c[0] == "news"] == [("news", "AAPL")]
    assert calls.index("news") < calls.index("submit")
    assert {r.manifest.schedule_digest for r in (agent, momentum, cash)} == {
        agent.manifest.schedule_digest
    }
    assert {o.request.symbol for o in momentum.orders} == MOMENTUM_BUYS
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


async def test_demo_agent_news_error_feedback_holds_without_order(tmp_path):
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import (
        AGENT_FIXTURE_INSTRUCTIONS,
        fixture_model_factory,
        make_agent_decider,
    )
    from bazaar_runner.agent_step import AgentStep

    agent_market = InMemoryMarket()
    served = delegating_transport(agent_market, approval_id=AGENT.approval_id)

    async def news_down(request):
        if "/news/" in request.url.path:
            return httpx.Response(500, json={"error": {"code": "internal_error", "message": "x"}})
        return await served.handle_async_request(request)

    step = AgentStep(
        make_agent_decider(AGENT_FIXTURE_INSTRUCTIONS, fixture_model_factory()),
        market_url="http://market",
        transport=httpx.MockTransport(news_down),
    )
    (agent,) = await run_demo(
        [AGENT],
        {AGENT: agent_market},
        {AGENT_FIXTURE_REF: lambda bundle, prices: step},
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )
    assert agent.status == "completed" and agent.orders == ()
    assert agent.decision_errors == ()
    assert [c[0] for c in agent_market.calls].count("submit") == 0


async def test_demo_agent_still_buys_once_when_the_news_archive_is_large(tmp_path):
    """T7b: 100 real-size articles must not exhaust the decision's token budget before the buy."""
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import (
        AGENT_FIXTURE_INSTRUCTIONS,
        FIXTURE_NEWS_LIMIT,
        fixture_model_factory,
        make_agent_decider,
    )
    from bazaar_runner.agent_step import AgentStep

    agent_market = InMemoryMarket()
    step = AgentStep(
        make_agent_decider(AGENT_FIXTURE_INSTRUCTIONS, fixture_model_factory()),
        market_url="http://market",
        transport=delegating_transport(
            agent_market, approval_id=AGENT.approval_id, news_articles=100
        ),
    )
    (agent,) = await run_demo(
        [AGENT],
        {AGENT: agent_market},
        {AGENT_FIXTURE_REF: lambda bundle, prices: step},
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        **DEMO,
    )
    assert agent.status == "completed" and agent.decision_errors == ()
    (order,) = agent.orders
    assert order.result.status == "filled"
    assert order.result.client_order_id == uuid5(AGENT.experiment_id, "decision:0")
    assert [c for c in agent_market.calls if c[0] == "news"] == [
        ("news", "AAPL", str(FIXTURE_NEWS_LIMIT))
    ]


def test_the_trading_day_counts_the_demo_sessions():
    from bazaar_runner.clock import trading_day

    sessions = BUNDLE.sessions
    first, last = sessions[0], sessions[-1]
    assert trading_day(sessions, first.open_at) == (1, 10, date(2026, 2, 2))
    assert trading_day(sessions, last.open_at) == (10, 10, date(2026, 2, 2))
    assert last.date == date(2026, 2, 13)
    assert trading_day(sessions, first.open_at - timedelta(minutes=1)) is None
    assert trading_day(sessions, first.close_at + timedelta(minutes=1)) is None


def baseline_policies():
    baselines = pytest.importorskip("bazaar_replay.baselines")
    return {
        MOMENTUM_REF: lambda bundle, prices: ScriptedMomentum(bundle.symbols, prices),
        CASH_ONLY_REF: lambda bundle, prices: baselines.CashOnly(),
        BUY_AND_HOLD_REF: lambda bundle, prices: baselines.BuyAndHold(bundle.symbols, prices),
    }


async def run_baselines(runs_dir, markets=None):
    launches = [MOMENTUM, CASH, HOLD]
    markets = markets or {launch: InMemoryMarket() for launch in launches}
    return await run_demo(
        launches,
        markets,
        baseline_policies(),
        starting_cash=Decimal(10000),
        runs_dir=runs_dir,
        **DEMO,
    )


async def test_buy_and_hold_buys_all_twelve_on_day_one_and_never_overdraws(tmp_path):
    *_, hold = await run_baselines(tmp_path)
    assert hold.status == "completed"
    assert sorted(o.request.symbol for o in hold.orders) == sorted(SYMBOLS)
    assert len(SYMBOLS) == 12
    # Day 1 only: every order at the first open, whole shares, all filled.
    assert {o.decided_at for o in hold.orders} == {SESSIONS[0].open_at}
    assert all(o.result.status == "filled" and o.request.quantity >= 1 for o in hold.orders)
    # About $833 a slot: even the priciest fixture stocks (META, MSFT) get a share.
    assert {o.request.symbol for o in hold.orders} >= {"META", "MSFT"}
    assert all(o.result.account.cash >= 0 for o in hold.orders)
    assert Decimal(0) <= hold.final_account.cash < Decimal(10000) / 12


async def test_two_identical_runs_on_twelve_symbols_give_identical_records(tmp_path):
    first = await run_baselines(tmp_path / "first")
    second = await run_baselines(tmp_path / "second")

    # Everything but the trace id, which names each run's own Logfire trace.
    def dumped(records):
        return [r.model_dump(mode="json", exclude={"trace_id"}) for r in records]

    assert dumped(first) == dumped(second)
    assert sum(len(r.orders) for r in first) > 12


async def test_every_record_json_carries_the_marked_value_series(tmp_path):
    """The web chart draws record.json's marks: one value per session for every completed run,
    including the baselines, and the sessions marked so far for a run that failed part way."""
    failing = InMemoryMarket()
    original = failing.set_cutoff
    cutoffs = []

    async def fails_on_day_four(experiment_id, cutoff, *versions):
        cutoffs.append(cutoff)
        if len(cutoffs) == 8:  # the 4th session's open: three closes are already marked
            raise MissingPriceForTest()
        return await original(experiment_id, cutoff, *versions)

    failing.set_cutoff = fails_on_day_four
    markets = {MOMENTUM: failing, CASH: InMemoryMarket(), HOLD: InMemoryMarket()}
    momentum, *_ = await run_baselines(tmp_path, markets)
    on_disk = {
        record["manifest"]["policy_ref"]: record
        for record in (json.loads(p.read_text()) for p in tmp_path.glob("*/record.json"))
    }
    assert len(on_disk) == 3 and momentum.status == "failed"
    for policy_ref in (CASH_ONLY_REF, BUY_AND_HOLD_REF):
        marks = on_disk[policy_ref]["marks"]
        assert len(marks) == len(SESSIONS) == 10
        assert all(Decimal(m["snapshot"]["portfolio_value"]) > 0 for m in marks)
    assert {Decimal(m["snapshot"]["portfolio_value"]) for m in on_disk[CASH_ONLY_REF]["marks"]} == {
        Decimal(10000)
    }
    partial = on_disk[MOMENTUM_REF]
    assert partial["status"] == "failed"
    assert [m["event_sequence"] for m in partial["marks"]] == [1, 3, 5]


class MissingPriceForTest(Exception):
    """A market failure part way through a run."""


def test_the_demo_agent_launch_keeps_the_default_decision_budget(monkeypatch):
    """Only submissions get the larger budget; the CLI's agent launch passes none, so
    run_decision uses DecisionBudget() (4 requests, 16k tokens, 20 tool calls)."""
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner import agent

    made = []
    monkeypatch.setattr(agent, "make_agent_decider", lambda *a, **kw: made.append(kw))
    policies = cli.load_policies("http://market")
    policies[AGENT_FIXTURE_REF](BUNDLE, None)
    (kwargs,) = made
    assert kwargs.get("budget") is None


ELEVEN = fixture_bundle("demo-bundle-eleven", tuple(s for s in SYMBOLS if s != "EA"))


class ReadingMarket(InMemoryMarket):
    def __init__(self) -> None:
        super().__init__()
        self.read: set[str] = set()

    async def price_at(self, symbol, cutoff):
        self.read.add(symbol)
        return await super().price_at(symbol, cutoff)


MOMENTUM2 = Launch(MOMENTUM_V2_REF, UUID(int=0xE6), UUID(int=0xA6))
HOLD_SPY = Launch(BUY_AND_HOLD_SPY_REF, UUID(int=0xE7), UUID(int=0xA7))
ELEVEN_SPY = fixture_bundle("demo-bundle-eleven", ELEVEN.symbols, benchmark="SPY")


class MovingMarket(InMemoryMarket):
    """EA (outside the eleven) is the day's top mover; KO rises to day 3, then falls."""

    def close(self, symbol, day):
        if symbol == "EA":
            return Decimal(150 + 10 * day)
        if symbol == "KO":
            return Decimal(60 + day if day <= 3 else 63 - 2 * (day - 3))
        return super().close(symbol, day)


async def run_cli_set(tmp_path, monkeypatch, markets=None, bundle=ELEVEN_SPY):
    from bazaar_runner import agent, agent_step

    made, steps = [], []
    monkeypatch.setattr(agent, "make_agent_decider", lambda *a, **kw: made.append(kw))
    monkeypatch.setattr(agent_step, "AgentStep", lambda *a, **kw: steps.append(kw) or cash_only)
    launches = [MOMENTUM2, CASH, HOLD_SPY, AGENT]
    markets = markets or {launch: MovingMarket() for launch in launches}
    records = await run_demo(
        launches,
        markets,
        cli.load_policies("http://market"),
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        bundle=bundle,
        execution_rule_version="exec-v1",
    )
    return records, markets, made, steps


async def test_the_cli_set_runs_on_an_eleven_symbol_bundle(monkeypatch, tmp_path):
    """The universe and benchmark come from the bundle: nothing orders a dropped symbol."""
    pytest.importorskip("bazaar_replay.baselines")
    pytest.importorskip("bazaar_agent.trading")
    records, _, made, steps = await run_cli_set(tmp_path, monkeypatch)

    assert all(
        r.status == "completed" and r.manifest.data_version == ELEVEN.bundle_id for r in records
    ), [r.failure for r in records]
    (decider,) = made
    assert decider["sessions"] == ELEVEN.sessions and "quote_symbols" not in decider
    # The agent has no eager universe read: cycles are read per filings request.
    assert len(steps) == 1 and "symbols" not in steps[0]
    # EA tops every screen but is not in the bundle, so nothing ever orders it.
    assert all(o.request.symbol != "EA" for r in records for o in r.orders)


async def test_buy_and_hold_spy_buys_only_the_benchmark_on_day_one(monkeypatch, tmp_path):
    pytest.importorskip("bazaar_replay.baselines")
    pytest.importorskip("bazaar_agent.trading")
    (_, _, hold, _), *_ = await run_cli_set(tmp_path, monkeypatch)

    (order,) = hold.orders
    # The first open sees the 2026-01-30 close, 400: floor(10000 / 400) = 25 whole shares.
    assert (order.request.symbol, order.request.side, order.request.quantity) == ("SPY", "buy", 25)
    assert order.decided_at == SESSIONS[0].open_at and order.result.status == "filled"
    assert hold.final_account.cash == 0 and len(hold.marks) == len(SESSIONS)


async def test_buy_and_hold_spy_fails_loudly_without_a_benchmark(monkeypatch, tmp_path):
    pytest.importorskip("bazaar_replay.baselines")
    pytest.importorskip("bazaar_agent.trading")
    with pytest.raises(ValueError, match="no benchmark"):
        await run_cli_set(tmp_path, monkeypatch, bundle=ELEVEN)


async def test_momentum_v2_screens_once_per_decision_and_follows_the_movers(monkeypatch, tmp_path):
    pytest.importorskip("bazaar_replay.baselines")
    pytest.importorskip("bazaar_agent.trading")
    (momentum, *_), markets, _, _ = await run_cli_set(tmp_path, monkeypatch)
    calls = markets[MOMENTUM2].calls

    # Exactly one screen per decision, the top 5 by 1d change; quotes only for holdings.
    screens = [c for c in calls if c[:2] == ("market", "screen")]
    assert len(screens) == len(SESSIONS)
    assert {tuple(sorted(c[2].items())) for c in screens} == {
        (("direction", "desc"), ("limit", "5"), ("sort", "pct_change_1d"))
    }
    assert not [c for c in calls if c[0] == "market" and c[1] not in ("screen", "quotes")]
    # No per-stock price reads: the screen and quotes carry every price it uses.
    orders = [(o.decided_at, o.request.side.value, o.request.symbol) for o in momentum.orders]
    # Day 1 has no 1d change yet; day 2 buys the top two movers in the universe, KO and FISV.
    assert orders[:2] == [(SESSIONS[1].open_at, "buy", "KO"), (SESSIONS[1].open_at, "buy", "FISV")]
    # KO falls after day 3, so it is sold at the next decision that sees the fall.
    assert ("sell", "KO") in {(side, symbol) for _, side, symbol in orders}
    assert all(o.result.status == "filled" for o in momentum.orders)
    assert all(o.result.account.cash >= 0 for o in momentum.orders)
    assert {symbol for *_, symbol in orders} <= set(ELEVEN.symbols)
    # At most two new holdings per decision.
    buys_per_day = {}
    for at, side, _ in orders:
        buys_per_day[at] = buys_per_day.get(at, 0) + (side == "buy")
    assert max(buys_per_day.values()) <= 2


async def test_momentum_v2_is_deterministic(monkeypatch, tmp_path):
    pytest.importorskip("bazaar_replay.baselines")
    pytest.importorskip("bazaar_agent.trading")
    first, *_ = await run_cli_set(tmp_path / "a", monkeypatch)
    second, *_ = await run_cli_set(tmp_path / "b", monkeypatch)

    def dumped(records):
        return [r.model_dump(mode="json", exclude={"trace_id"}) for r in records]

    assert dumped(first) == dumped(second) and first[0].orders


async def test_the_agent_fixture_runs_on_a_500_symbol_bundle(tmp_path):
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import (
        AGENT_FIXTURE_INSTRUCTIONS,
        fixture_model_factory,
        make_agent_decider,
    )
    from bazaar_runner.agent_step import AgentStep

    universe = (*SYMBOLS, *(f"S{i:03d}" for i in range(500 - len(SYMBOLS))))
    big = fixture_bundle("demo-bundle-500", universe, benchmark="SPY")
    market = InMemoryMarket()
    (record,) = await run_demo(
        [AGENT],
        {AGENT: market},
        {
            AGENT_FIXTURE_REF: lambda bundle, prices: AgentStep(
                make_agent_decider(
                    AGENT_FIXTURE_INSTRUCTIONS, fixture_model_factory(), sessions=bundle.sessions
                ),
                market_url="http://market",
                transport=delegating_transport(market, approval_id=AGENT.approval_id),
            )
        },
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
        bundle=big,
        execution_rule_version="exec-v1",
    )
    assert len(big.symbols) == 500
    assert record.status == "completed" and record.decision_errors == (), record.failure
    (order,) = record.orders
    assert (order.request.symbol, order.result.status) == ("AAPL", "filled")
    # The summary reads stay bounded: one sectors and two screens per decision.
    reads = [c[1] for c in market.calls if c[0] == "market"]
    assert reads.count("sectors") == len(SESSIONS) and reads.count("screen") == 2 * len(SESSIONS)


async def test_the_twelve_symbol_bundle_still_drives_buy_and_hold(tmp_path):
    *_, hold = await run_baselines(tmp_path)
    assert len(BUNDLE.symbols) == 12 and hold.manifest.data_version == BUNDLE.bundle_id
    assert sorted(o.request.symbol for o in hold.orders) == sorted(BUNDLE.symbols)


def test_the_cli_bundle_defaults_to_the_operator_env(offline_cli, monkeypatch):
    assert cli.parse_args(["--demo", *cli_ids()]).data_version == "demo-bundle-v2"
    monkeypatch.setenv("BAZAAR_DATA_BUNDLE", "")
    assert cli.parse_args(["--demo", *cli_ids()]).data_version == "demo-bundle-v2"
    monkeypatch.setenv("BAZAAR_DATA_BUNDLE", "demo-bundle-v1")
    assert cli.parse_args(["--demo", *cli_ids()]).data_version == "demo-bundle-v1"
    # An explicit flag still wins over the environment.
    args = cli.parse_args(["--demo", "--data-version", "synthetic-v1", *cli_ids()])
    assert args.data_version == "synthetic-v1"


@pytest.mark.parametrize("flag", [False, True])
def test_a_malformed_bundle_id_stops_the_cli_before_anything(
    offline_cli, monkeypatch, capsys, tmp_path, flag
):
    monkeypatch.setenv(RUNNER_TOKEN_ENV, TOKEN)
    extra = ["--data-version", "../v1"] if flag else []
    if not flag:
        monkeypatch.setenv("BAZAAR_DATA_BUNDLE", "../v1")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--demo", "--runs-dir", str(tmp_path), *extra, *cli_ids()])
    assert exit_.value.code == 2
    err = capsys.readouterr().err
    assert ("letters, digits" if flag else "BAZAAR_DATA_BUNDLE is not a bundle id") in err
    assert "../v1" not in err
    assert list(tmp_path.iterdir()) == []
