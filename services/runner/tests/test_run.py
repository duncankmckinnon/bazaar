from decimal import Decimal
from uuid import uuid5

import pytest
from bazaar_protocol import ErrorCode, ErrorDetail, OrderRequest, OrderSide
from bazaar_runner.market import ApprovalDenied, MarketError
from bazaar_runner.run import RunResult, RunState, run_strategy

from .market_fakes import SESSIONS, SPEC, InMemoryMarket, refused_sentence


def order(side: OrderSide, symbol: str, quantity: int, n: int) -> OrderRequest:
    return OrderRequest(
        client_order_id=uuid5(SPEC.run_id, str(n)), symbol=symbol, side=side, quantity=quantity
    )


# Whole shares only. Prices are the previous close, since each decision is at the open.
SCRIPTED = {
    # 2026-02-02, sees the 01-30 close: buy 10 AAPL at 200 for 2000.
    0: (order(OrderSide.BUY, "AAPL", 10, 0),),
    # 2026-02-03: an unaffordable MSFT buy is rejected, then buy 20 KO at 61 for 1220.
    2: (order(OrderSide.BUY, "MSFT", 1000, 1), order(OrderSide.BUY, "KO", 20, 2)),
    # 2026-02-04: sell 4 AAPL at 202 for 808.
    4: (order(OrderSide.SELL, "AAPL", 4, 3),),
}


def scripted_policy(seen: list):
    async def decide(ctx, account):
        seen.append((ctx, account))
        return SCRIPTED.get(ctx.event_sequence, ())

    return decide


def holdings(result: RunResult) -> dict[str, Decimal]:
    return {h.symbol: h.quantity for h in result.account.holdings}


async def test_scripted_run_completes_end_to_end():
    market, seen = InMemoryMarket(), []
    result = await run_strategy(SPEC, market, scripted_policy(seen))

    assert result.state is RunState.COMPLETED
    assert result.failure is None and result.failure_code is None
    assert result.account.cash == Decimal(7588)
    assert holdings(result) == {"AAPL": Decimal(6), "KO": Decimal(20)}
    assert [(o.event_sequence, o.order_index, o.result.status) for o in result.orders] == [
        (0, 0, "filled"),
        (2, 0, "rejected"),
        (2, 1, "filled"),
        (4, 0, "filled"),
    ]
    assert [o.result.unit_price for o in result.orders if o.result.status == "filled"] == [
        Decimal(200),
        Decimal(61),
        Decimal(202),
    ]
    assert [ctx.event_sequence for ctx, _ in seen] == list(range(0, 20, 2))
    assert [m.event_sequence for m in result.marks] == list(range(1, 20, 2))
    values = [m.snapshot.portfolio_value for m in result.marks]
    # 02-02 close: 8000 + 10*201. 02-03 close: 6780 + 10*202 + 20*62. Last: 7588 + 6*210 + 20*70.
    assert (values[0], values[1], values[-1]) == (Decimal(10010), Decimal(10040), Decimal(10248))
    assert RunResult.model_validate_json(result.model_dump_json()) == result


async def test_market_calls_follow_the_clock():
    market = InMemoryMarket()
    result = await run_strategy(SPEC, market, scripted_policy([]))

    kinds = [call[0] for call in market.calls]
    assert kinds[:2] == ["set_cutoff", "create_account"]
    assert market.calls[0][1] == SESSIONS[0].open_at
    # Every DECISION and MARK event moves the cutoff first; a mark reads only the current cutoff.
    cutoffs = [call[1] for call in market.calls if call[0] == "set_cutoff"][1:]
    assert cutoffs == [t for s in SESSIONS for t in (s.open_at, s.close_at)]
    for i, kind in enumerate(kinds):
        if kind in ("account", "portfolio"):
            assert kinds[i - 1] == "set_cutoff" and market.calls[i - 1][1] == market.calls[i][1]
    assert kinds[-1] == "close_account"
    assert result.account.account_id in market.closed


async def test_decisions_never_see_a_later_cutoff_account_or_price():
    market, seen = InMemoryMarket(), []
    scripted = scripted_policy(seen)

    async def decide(ctx, account):
        assert market.cutoff == ctx.simulated_at
        assert account.simulated_at <= ctx.simulated_at
        price = await market.price_at("AAPL", ctx.simulated_at)
        assert price.available_at < ctx.simulated_at
        return await scripted(ctx, account)

    result = await run_strategy(SPEC, market, decide)
    assert result.state is RunState.COMPLETED


async def test_rejected_order_is_recorded_and_the_run_continues():
    result = await run_strategy(SPEC, InMemoryMarket(), scripted_policy([]))
    rejected = [o.result for o in result.orders if o.result.status == "rejected"]
    assert [r.error.code for r in rejected] == [ErrorCode.INSUFFICIENT_CASH]
    assert rejected[0].account.cash == Decimal(8000)
    assert result.state is RunState.COMPLETED
    assert len(result.marks) == len(SESSIONS)


async def test_decide_raising_fails_the_run_and_keeps_its_fills():
    market, seen = InMemoryMarket(), []
    scripted = scripted_policy(seen)

    async def decide(ctx, account):
        if ctx.event_sequence == 6:
            raise RuntimeError("policy blew up")
        return await scripted(ctx, account)

    result = await run_strategy(SPEC, market, decide)

    assert result.state is RunState.FAILED
    # 2026-02-05 is the fourth session; its open is event 6.
    assert result.failure == (
        "the run stopped on RuntimeError during the decision at 2026-02-05T14:30:00Z (event 6)"
    )
    assert "policy blew up" not in result.failure
    assert result.failure_code == "policy_error"
    assert [o.event_sequence for o in result.orders] == [0, 2, 2, 4]
    assert [m.event_sequence for m in result.marks] == [1, 3, 5]
    # The losses and holdings stay as settled; nothing is rolled back.
    assert result.account.cash == Decimal(7588)
    assert holdings(result) == {"AAPL": Decimal(6), "KO": Decimal(20)}
    assert result.account.account_id in market.closed
    assert [c[0] for c in market.calls].count("set_cutoff") == 1 + 7


@pytest.mark.parametrize("fail_on", ["set_cutoff", "create_account"])
async def test_market_error_before_any_account_fails_the_run(fail_on):
    market = InMemoryMarket()

    async def refuse(*args, **kwargs):
        raise ApprovalDenied("This approval does not allow the call")

    setattr(market, fail_on, refuse)
    result = await run_strategy(SPEC, market, scripted_policy([]))
    assert result.state is RunState.FAILED
    assert result.account is None and result.orders == () and result.marks == ()
    assert result.failure_code == "approval_denied"
    assert result.failure == refused_sentence(SPEC.approval_id, SPEC.experiment_id)
    assert "close_account" not in [c[0] for c in market.calls]


async def test_close_failure_is_recorded_and_the_run_is_failed():
    market = InMemoryMarket()

    async def broken_close(*args):
        raise MarketError(ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="down"))

    market.close_account = broken_close
    result = await run_strategy(SPEC, market, scripted_policy([]))
    assert result.state is RunState.FAILED
    assert result.failure == (
        "while closing the account, the market said: down"
        f"; account {result.account.account_id} was left open"
    )
    assert result.failure_code is ErrorCode.INTERNAL_ERROR
    assert result.account.cash == Decimal(7588)


async def test_a_policy_reading_a_future_price_fails_the_run():
    market = InMemoryMarket()

    async def peek(ctx, account):
        await market.price_at("AAPL", SESSIONS[-1].close_at)
        return ()

    result = await run_strategy(SPEC, market, peek)
    assert result.state is RunState.FAILED
    assert result.failure_code == "future_data"
    assert result.failure == (
        "a read past the experiment's clock was refused"
        " during the decision at 2026-02-02T14:30:00Z (event 0)"
    )
    assert result.orders == () and result.marks == ()
    assert result.account.account_id in market.closed


async def test_the_driver_never_moves_the_clock_past_period_end(monkeypatch):
    from datetime import timedelta

    from bazaar_runner import run
    from bazaar_runner.clock import EventKind, ScheduledEvent, build_schedule

    def one_too_many(script):
        schedule = build_schedule(script)
        late = ScheduledEvent(
            kind=EventKind.MARK,
            simulated_at=SESSIONS[-1].close_at + timedelta(days=1),
            event_sequence=len(schedule),
        )
        return (*schedule, late)

    monkeypatch.setattr(run, "build_schedule", one_too_many)
    market = InMemoryMarket()
    result = await run_strategy(SPEC, market, scripted_policy([]))

    assert result.state is RunState.FAILED
    assert result.failure_code == "period_overrun"
    assert result.failure == (
        "the runner stopped during the mark at 2026-02-14T21:00:00Z (event 20) instead of moving"
        " the clock past period_end (2026-02-14T21:00:00Z is after 2026-02-13T21:00:00Z)"
    )
    cutoffs = [c[1] for c in market.calls if c[0] == "set_cutoff"]
    assert max(cutoffs) == SESSIONS[-1].close_at
    # Everything up to the real period_end settled and is kept.
    assert len(result.marks) == len(SESSIONS)
    assert result.account.account_id in market.closed
