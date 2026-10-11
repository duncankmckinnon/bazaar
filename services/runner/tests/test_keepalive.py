"""The level-15 incident: a mark after a long decision reused a pooled connection the market was
closing, and the runner turned the ReadError into a failed run. Reproduced offline: the transport
serves a request, then drops the next one on the reused connection, then serves again."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from bazaar_protocol import ErrorCode, OrderRequest, OrderSide
from bazaar_runner import http_market
from bazaar_runner.demo import CASH_ONLY_REF, Launch, run_demo
from bazaar_runner.http_market import KEEPALIVE_EXPIRY_SECONDS, HttpMarketPort, market_client
from bazaar_runner.market import MarketError

from .market_fakes import SPEC, TOKEN, InMemoryMarket, delegating_transport

LAUNCH = Launch(CASH_ONLY_REF, SPEC.experiment_id, SPEC.approval_id)
# uvicorn's default timeout_keep_alive, which the market runs with.
SERVER_KEEPALIVE_SECONDS = 5


class DroppingTransport(httpx.AsyncBaseTransport):
    """The in-memory market, except that requests matching `drop` raise `error` (as when the
    server closes a reused keep-alive connection) up to `times` times in all."""

    def __init__(
        self,
        drop: Callable[[httpx.Request], bool],
        *,
        times: int = 1,
        error: type[httpx.TransportError] = httpx.ReadError,
    ) -> None:
        self.fake = InMemoryMarket()
        self.served = delegating_transport(self.fake)
        self.drop, self.left, self.error = drop, times, error
        self.seen: list[tuple[str, str, bool]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        dropped = self.left > 0 and self.drop(request)
        self.seen.append((request.method, request.url.path, dropped))
        if dropped:
            self.left -= 1
            raise self.error("Server disconnected without sending a response.", request=request)
        return await self.served.handle_async_request(request)


def port_on(transport: DroppingTransport) -> HttpMarketPort:
    client = httpx.AsyncClient(transport=transport, base_url="http://market")
    return HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, TOKEN)


def portfolio_get(request: httpx.Request) -> bool:
    return request.method == "GET" and request.url.path.endswith("/portfolio")


async def cash_only(ctx, account):
    return ()


async def test_a_mark_survives_one_dropped_connection(capfire, tmp_path: Path):
    transport = DroppingTransport(portfolio_get)
    (record,) = await run_demo(
        [LAUNCH],
        {LAUNCH: port_on(transport)},
        {CASH_ONLY_REF: lambda prices: cash_only},
        data_version="synthetic-v1",
        execution_rule_version="exec-v1",
        starting_cash=Decimal(10000),
        runs_dir=tmp_path,
    )

    assert record.status == "completed" and len(record.marks) == 10, record.failure
    # The first mark's portfolio read was dropped once and sent again, nothing else repeated.
    portfolio = [s for s in transport.seen if s[1].endswith("/portfolio")]
    assert [dropped for *_, dropped in portfolio[:2]] == [True, False]
    assert sum(dropped for *_, dropped in transport.seen) == 1
    spans = capfire.exporter.exported_spans_as_dict()
    (retry,) = [s for s in spans if s["attributes"].get("retried") == 1]
    assert retry["attributes"]["error_type"] == "ReadError"
    assert retry["attributes"]["method"] == "GET"
    assert retry["attributes"]["path"].endswith("/portfolio")
    by_id = {s["context"]["span_id"]: s for s in spans}
    assert by_id[retry["parent"]["span_id"]]["name"] == "runner.mark"
    assert TOKEN not in json.dumps(spans, default=str)


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError])
async def test_each_dropped_connection_error_retries_a_get_once(error):
    transport = DroppingTransport(lambda r: r.method == "GET", error=error)
    port = port_on(transport)
    at = SPEC.script.sessions[0].open_at
    await port.set_cutoff(SPEC.experiment_id, at, "synthetic-v1", "exec-v1")
    observation = await port.price_at("AAPL", at)
    assert observation.price == Decimal(200)
    assert [(m, d) for m, _, d in transport.seen] == [("PUT", False), ("GET", True), ("GET", False)]


async def test_a_get_that_drops_twice_fails():
    transport = DroppingTransport(lambda r: r.method == "GET", times=2)
    with pytest.raises(MarketError) as caught:
        await port_on(transport).price_at("AAPL", SPEC.script.sessions[0].open_at)
    assert caught.value.detail.code is ErrorCode.INTERNAL_ERROR
    assert "ReadError" in caught.value.detail.message
    assert [d for *_, d in transport.seen] == [True, True]


async def test_a_timeout_is_not_retried():
    transport = DroppingTransport(lambda r: r.method == "GET", error=httpx.ReadTimeout)
    with pytest.raises(MarketError, match="ReadTimeout"):
        await port_on(transport).price_at("AAPL", SPEC.script.sessions[0].open_at)
    assert len(transport.seen) == 1


async def test_an_order_post_is_never_resent():
    transport = DroppingTransport(lambda r: r.method == "POST" and r.url.path.endswith("/orders"))
    port = port_on(transport)
    await port.set_cutoff(SPEC.experiment_id, SPEC.script.sessions[0].open_at, "s", "exec-v1")
    account = await port.create_account(
        SPEC.experiment_id,
        SPEC.agent_id,
        SPEC.strategy_version_id,
        Decimal(1000),
        request_id=UUID(int=7),
    )
    from bazaar_protocol import ExperimentContext

    ctx = ExperimentContext(
        experiment_id=SPEC.experiment_id,
        account_id=account.account_id,
        agent_id=SPEC.agent_id,
        strategy_version_id=SPEC.strategy_version_id,
        approval_id=SPEC.approval_id,
        data_version="s",
        execution_rule_version="exec-v1",
        simulated_at=SPEC.script.sessions[0].open_at,
        event_sequence=0,
    )
    order = OrderRequest(client_order_id=UUID(int=8), symbol="AAPL", side=OrderSide.BUY, quantity=1)
    with pytest.raises(MarketError, match="ReadError"):
        await port.submit(ctx, order)
    assert [s[:2] for s in transport.seen if s[0] == "POST" and s[1].endswith("/orders")] == [
        ("POST", f"/experiments/{SPEC.experiment_id}/accounts/{account.account_id}/orders")
    ]


async def test_a_control_put_is_never_resent():
    transport = DroppingTransport(lambda r: r.method == "PUT")
    with pytest.raises(MarketError, match="ReadError"):
        await port_on(transport).set_cutoff(
            SPEC.experiment_id, SPEC.script.sessions[0].open_at, "s", "exec-v1"
        )
    assert len(transport.seen) == 1


async def test_market_clients_expire_idle_connections_before_the_server_does():
    assert KEEPALIVE_EXPIRY_SECONDS < SERVER_KEEPALIVE_SECONDS
    async with market_client("http://market", timeout=30) as client:
        # httpx keeps the limit on its connection pool; there is no public accessor.
        assert client._transport._pool._keepalive_expiry == KEEPALIVE_EXPIRY_SECONDS


def test_every_runner_market_client_is_built_by_market_client():
    """The CLI, submissions and each agent decision all build their client through
    market_client, so none can fall back to httpx's 5 s default."""
    source = Path(http_market.__file__).parent
    constructions = {
        path.name: path.read_text().count("httpx.AsyncClient(")
        for path in source.rglob("*.py")
        if "httpx.AsyncClient(" in path.read_text()
    }
    assert constructions == {"http_market.py": 1}
    users = {p.name for p in source.rglob("*.py") if "market_client(" in p.read_text()}
    assert users >= {"__main__.py", "submission.py", "agent_step.py"}
