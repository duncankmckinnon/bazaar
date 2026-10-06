import json
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from bazaar_protocol import OrderRequest, OrderSide
from bazaar_runner import __main__ as cli
from bazaar_runner.demo import (
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

from .market_fakes import SESSIONS, SPEC, TOKEN, InMemoryMarket, delegating_transport

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
    # The refused run has no account, so evals cannot read it yet (record.evaluable).
    assert len(list(tmp_path.glob("*/evaluation.json"))) == 0


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


def test_cli_stops_at_startup_without_the_token(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "configure_telemetry", lambda: None)
    monkeypatch.delenv(RUNNER_TOKEN_ENV, raising=False)
    ids = [
        arg
        for n, name in enumerate(cli.APPROVED_RUNS)
        for arg in (
            f"--{name}-experiment-id",
            str(UUID(int=n)),
            f"--{name}-approval-id",
            str(UUID(int=n)),
        )
    ]
    code = cli.main(["--demo", "--data-version", "synthetic-v1", "--runs-dir", str(tmp_path), *ids])
    assert code == 2
    assert RUNNER_TOKEN_ENV in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_cli_requires_every_approved_run_id(monkeypatch, capsys):
    for name in cli.APPROVED_RUNS:
        for kind in ("EXPERIMENT", "APPROVAL"):
            monkeypatch.delenv(f"BAZAAR_{name.upper().replace('-', '_')}_{kind}_ID", raising=False)
    with pytest.raises(SystemExit):
        cli.parse_args(["--demo", "--data-version", "synthetic-v1"])
    assert "--cash-only-approval-id" in capsys.readouterr().err
