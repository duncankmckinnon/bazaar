import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid5

import httpx
import pytest
from bazaar_protocol import (
    AccountSnapshot,
    ApiError,
    ErrorCode,
    ErrorDetail,
    FilledOrder,
    Holding,
    OrderRequest,
    OrderSide,
    PriceHistory,
    PriceObservation,
    RejectedOrder,
)
from bazaar_runner.http_market import (
    APPROVAL_HEADER,
    RUNNER_TOKEN_ENV,
    RUNNER_TOKEN_HEADER,
    HttpMarketPort,
    RunnerConfigError,
    utc_z,
)
from bazaar_runner.market import (
    ApprovalDenied,
    FutureData,
    MarketError,
    MissingPrice,
    RunnerUnauthorized,
)
from bazaar_runner.run import RunState, run_strategy

from .market_fakes import CLOSES, SESSIONS, SPEC, InMemoryMarket

EID, APPROVAL = SPEC.experiment_id, SPEC.approval_id
TOKEN = "runner-token-do-not-leak"
CONTROL_ROUTES = {"PUT /cutoff", "POST /accounts", "POST /close"}
ACCOUNT_ID = UUID("00000000-0000-0000-0000-0000000000b1")
OPEN = SESSIONS[0].open_at
ROOT = f"/experiments/{EID}"

ACCOUNT = AccountSnapshot(
    account_id=ACCOUNT_ID,
    agent_id=SPEC.agent_id,
    experiment_id=EID,
    strategy_version_id=SPEC.strategy_version_id,
    simulated_at=OPEN,
    state_version=0,
    cash="10000",
)
CTX = SimpleNamespace(experiment_id=EID, account_id=ACCOUNT_ID)
ORDER = OrderRequest(client_order_id=UUID(int=7), symbol="AAPL", side=OrderSide.BUY, quantity=1)


def model_response(status: int, model) -> httpx.Response:
    return httpx.Response(
        status, content=model.model_dump_json(), headers={"content-type": "application/json"}
    )


def api_error(status: int, code: ErrorCode, message: str = "refused") -> httpx.Response:
    return model_response(status, ApiError(error=ErrorDetail(code=code, message=message)))


def port_answering(*responses: httpx.Response) -> tuple[HttpMarketPort, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://market")
    return HttpMarketPort(client, EID, APPROVAL, TOKEN), seen


def only(seen: list[httpx.Request], method: str, path: str) -> httpx.Request:
    (request,) = seen
    assert (request.method, request.url.path) == (method, ROOT + path)
    assert request.headers[APPROVAL_HEADER] == str(APPROVAL)
    return request


async def test_set_cutoff_puts_a_z_cutoff_with_versions():
    port, seen = port_answering(
        httpx.Response(200, json={"experiment_id": str(EID), "cutoff": "2026-02-02T21:00:00Z"})
    )
    close = SESSIONS[0].close_at
    assert await port.set_cutoff(EID, close, "synthetic-v1", "exec-v1") == close
    request = only(seen, "PUT", "/cutoff")
    assert b'"cutoff":"2026-02-02T21:00:00Z"' in request.content
    assert json.loads(request.content) == {
        "cutoff": "2026-02-02T21:00:00Z",
        "data_version": "synthetic-v1",
        "execution_rule_version": "exec-v1",
    }


async def test_create_account_posts_the_request_and_parses_201():
    port, seen = port_answering(model_response(201, ACCOUNT))
    account = await port.create_account(
        EID, SPEC.agent_id, SPEC.strategy_version_id, Decimal(10000), request_id=SPEC.run_id
    )
    assert account == ACCOUNT
    assert json.loads(only(seen, "POST", "/accounts").content) == {
        "request_id": str(SPEC.run_id),
        "agent_id": str(SPEC.agent_id),
        "strategy_version_id": str(SPEC.strategy_version_id),
        "cash": "10000",
    }


async def test_account_gets_the_snapshot():
    port, seen = port_answering(model_response(200, ACCOUNT))
    assert await port.account(CTX) == ACCOUNT
    only(seen, "GET", f"/accounts/{ACCOUNT_ID}")


@pytest.mark.parametrize("filled", [True, False])
async def test_submit_posts_the_order_and_parses_either_result(filled):
    common = ORDER.model_dump() | {"order_id": UUID(int=8), "account": ACCOUNT}
    if filled:
        result = FilledOrder(
            **common,
            unit_price="200",
            fee="0",
            executed_at=OPEN,
            price_observed_at=CLOSES[0],
            price_available_at=CLOSES[0],
            price_source="fixture",
            data_version="synthetic-v1",
            execution_rule_version="exec-v1",
        )
    else:
        result = RejectedOrder(
            **common,
            rejected_at=OPEN,
            error={"code": "insufficient_cash", "message": "no cash"},
        )
    port, seen = port_answering(model_response(200, result))
    assert await port.submit(CTX, ORDER) == result
    request = only(seen, "POST", f"/accounts/{ACCOUNT_ID}/orders")
    assert json.loads(request.content) == ORDER.model_dump(mode="json")


async def test_portfolio_gets_the_snapshot():
    market = InMemoryMarket()
    await market.set_cutoff(EID, OPEN, "synthetic-v1", "exec-v1")
    account = await market.create_account(
        EID, SPEC.agent_id, SPEC.strategy_version_id, Decimal(100), request_id=SPEC.run_id
    )
    snapshot = await market.portfolio(
        SimpleNamespace(account_id=account.account_id, simulated_at=OPEN)
    )
    port, seen = port_answering(model_response(200, snapshot))
    assert await port.portfolio(CTX) == snapshot
    only(seen, "GET", f"/accounts/{ACCOUNT_ID}/portfolio")


async def test_close_account_posts_close():
    port, seen = port_answering(model_response(200, ACCOUNT))
    assert await port.close_account(EID, ACCOUNT_ID) == ACCOUNT
    only(seen, "POST", f"/accounts/{ACCOUNT_ID}/close")


def history(*closes: datetime, cursor: str | None = None) -> PriceHistory:
    return PriceHistory(
        experiment_id=EID,
        symbol="AAPL",
        cutoff_at=OPEN,
        source="fixture",
        data_version="synthetic-v1",
        observations=tuple(
            PriceObservation(observed_at=c, available_at=c, price=str(200 + i))
            for i, c in enumerate(closes)
        ),
        next_cursor=cursor,
    )


async def test_price_at_follows_the_cursor_and_returns_the_latest_close():
    older = CLOSES[0] - timedelta(days=1)
    port, seen = port_answering(
        model_response(200, history(older, cursor="page-2")),
        model_response(200, history(CLOSES[0])),
    )
    observation = await port.price_at("AAPL", OPEN)
    assert observation.observed_at == CLOSES[0]
    first, second = seen
    assert first.url.path == f"{ROOT}/prices/AAPL"
    assert all(r.headers[APPROVAL_HEADER] == str(APPROVAL) for r in seen)
    assert first.url.params["end_at"] == "2026-02-02T14:30:00Z"
    assert first.url.params["start_at"] == "2026-01-23T14:30:00Z"
    assert "cursor" not in first.url.params
    assert second.url.params["cursor"] == "page-2"


@pytest.mark.parametrize(
    "response",
    [model_response(200, history()), api_error(404, ErrorCode.NOT_FOUND, "No prices for ZZZ")],
)
async def test_price_at_without_a_close_raises_missing_price(response):
    port, _ = port_answering(response)
    with pytest.raises(MissingPrice):
        await port.price_at("AAPL", OPEN)


async def test_prices_403_forbidden_is_future_data():
    port, _ = port_answering(
        api_error(403, ErrorCode.FORBIDDEN, "end_at is after the experiment's current time")
    )
    with pytest.raises(FutureData) as future:
        await port.price_at("AAPL", OPEN)
    assert not isinstance(future.value, ApprovalDenied)
    assert future.value.detail.code is ErrorCode.FORBIDDEN
    assert future.value.detail.message == "end_at is after the experiment's current time"


async def test_prices_403_not_approved_is_approval_denied():
    port, _ = port_answering(api_error(403, ErrorCode.EXPERIMENT_NOT_APPROVED))
    with pytest.raises(ApprovalDenied) as denied:
        await port.price_at("AAPL", OPEN)
    assert not isinstance(denied.value, FutureData)


async def test_403_not_approved_is_approval_denied_on_control_routes_too():
    port, _ = port_answering(api_error(403, ErrorCode.EXPERIMENT_NOT_APPROVED))
    with pytest.raises(ApprovalDenied):
        await port.set_cutoff(EID, OPEN, "synthetic-v1", "exec-v1")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (api_error(409, ErrorCode.EXPERIMENT_NOT_RUNNING), ErrorCode.EXPERIMENT_NOT_RUNNING),
        (api_error(422, ErrorCode.INVALID_REQUEST), ErrorCode.INVALID_REQUEST),
        (api_error(401, ErrorCode.UNAUTHORIZED), ErrorCode.UNAUTHORIZED),
        # forbidden means FutureData only on the prices route.
        (api_error(403, ErrorCode.FORBIDDEN), ErrorCode.FORBIDDEN),
        (httpx.Response(502, text="bad gateway"), ErrorCode.INTERNAL_ERROR),
    ],
)
async def test_other_errors_keep_the_market_code(response, code):
    port, _ = port_answering(response)
    with pytest.raises(MarketError) as error:
        await port.submit(CTX, ORDER)
    assert type(error.value) is MarketError
    assert error.value.detail.code is code


async def test_a_transport_failure_is_one_attempt_and_a_market_error():
    attempts = []

    def unreachable(request):
        attempts.append(request)
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unreachable), base_url="http://m")
    with pytest.raises(MarketError) as error:
        await HttpMarketPort(client, EID, APPROVAL, TOKEN).submit(CTX, ORDER)
    assert error.value.detail.code is ErrorCode.INTERNAL_ERROR
    assert len(attempts) == 1


async def test_misuse_is_refused_before_any_request():
    port, seen = port_answering()
    with pytest.raises(NotImplementedError):
        await port.create_account(
            EID,
            SPEC.agent_id,
            SPEC.strategy_version_id,
            Decimal(1),
            (Holding(symbol="AAPL", quantity=1),),
            request_id=SPEC.run_id,
        )
    with pytest.raises(ValueError, match="UTC"):
        await port.set_cutoff(
            EID, OPEN.astimezone(timezone(timedelta(hours=-5))), "synthetic-v1", "exec-v1"
        )
    with pytest.raises(ValueError, match="bound to experiment"):
        await port.close_account(UUID(int=99), ACCOUNT_ID)
    assert seen == []


def route(request: httpx.Request) -> str:
    return f"{request.method} /{request.url.path.split('/')[-1]}"


def delegating_transport(
    fake: InMemoryMarket, seen: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    """Serves the market routes from the in-memory fake, so the adapter meets the driver."""

    async def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.headers.get(APPROVAL_HEADER) != str(SPEC.approval_id):
            return api_error(403, ErrorCode.EXPERIMENT_NOT_APPROVED)
        if route(request) in CONTROL_ROUTES and request.headers.get(RUNNER_TOKEN_HEADER) != TOKEN:
            return api_error(401, ErrorCode.UNAUTHORIZED, "runner token required")
        _, _, eid, *rest = request.url.path.split("/")
        eid = UUID(eid)
        body = json.loads(request.content) if request.content else {}
        view = SimpleNamespace(
            simulated_at=fake.cutoff,
            data_version=SPEC.data_version,
            execution_rule_version=SPEC.execution_rule_version,
        )
        try:
            match request.method, rest:
                case "PUT", ["cutoff"]:
                    cutoff = await fake.set_cutoff(
                        eid,
                        datetime.fromisoformat(body["cutoff"]),
                        body["data_version"],
                        body["execution_rule_version"],
                    )
                    return httpx.Response(
                        200, json={"experiment_id": str(eid), "cutoff": utc_z(cutoff)}
                    )
                case "POST", ["accounts"]:
                    account = await fake.create_account(
                        eid,
                        UUID(body["agent_id"]),
                        UUID(body["strategy_version_id"]),
                        Decimal(body["cash"]),
                        request_id=UUID(body["request_id"]),
                    )
                    return model_response(201, account)
                case "GET", ["accounts", aid]:
                    view.account_id = UUID(aid)
                    return model_response(200, await fake.account(view))
                case "POST", ["accounts", aid, "orders"]:
                    view.account_id = UUID(aid)
                    order = OrderRequest.model_validate(body)
                    return model_response(200, await fake.submit(view, order))
                case "GET", ["accounts", aid, "portfolio"]:
                    view.account_id = UUID(aid)
                    return model_response(200, await fake.portfolio(view))
                case "POST", ["accounts", aid, "close"]:
                    return model_response(200, await fake.close_account(eid, UUID(aid)))
                case "GET", ["prices", symbol]:
                    end_at = datetime.fromisoformat(request.url.params["end_at"])
                    observation = await fake.price_at(symbol, end_at)
                    page = history().model_copy(
                        update={"cutoff_at": fake.cutoff, "observations": (observation,)}
                    )
                    return model_response(200, page)
        except MissingPrice as exc:
            return api_error(404, ErrorCode.NOT_FOUND, exc.detail.message)
        except MarketError as exc:
            status = 403 if exc.detail.code is ErrorCode.FORBIDDEN else 409
            return api_error(status, exc.detail.code, exc.detail.message)
        return api_error(404, ErrorCode.NOT_FOUND, "no route")

    return httpx.MockTransport(handle)


def buy_and_hold(price_at):
    """Spend half the cash on AAPL and a quarter on KO at the first open, priced through PriceAt."""

    async def decide(ctx, account):
        if ctx.event_sequence != 0:
            return ()
        orders = []
        for n, (symbol, share) in enumerate((("AAPL", 2), ("KO", 4))):
            price = (await price_at(symbol, ctx.simulated_at)).price
            quantity = int(account.cash / share / price)
            orders.append(
                OrderRequest(
                    client_order_id=uuid5(SPEC.run_id, str(n)),
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=quantity,
                )
            )
        return tuple(orders)

    return decide


async def test_the_driver_runs_over_http_exactly_as_over_the_port():
    direct = InMemoryMarket()
    expected = await run_strategy(SPEC, direct, buy_and_hold(direct.price_at))

    fake = InMemoryMarket()
    client = httpx.AsyncClient(transport=delegating_transport(fake), base_url="http://market")
    port = HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, TOKEN)
    result = await run_strategy(SPEC, port, buy_and_hold(port.price_at))

    assert result.state is RunState.COMPLETED
    assert [o.result.status for o in result.orders] == ["filled", "filled"]
    assert result.orders[0].result.unit_price == Decimal(200)
    assert len(result.marks) == len(SESSIONS)
    assert result == expected
    assert fake.calls == direct.calls


async def test_an_unapproved_run_over_http_fails_as_approval_denied():
    fake = InMemoryMarket()
    client = httpx.AsyncClient(transport=delegating_transport(fake), base_url="http://market")
    port = HttpMarketPort(client, SPEC.experiment_id, UUID(int=404), TOKEN)
    result = await run_strategy(SPEC, port, buy_and_hold(port.price_at))

    assert result.state is RunState.FAILED
    assert result.failure_code == "approval_denied"
    assert result.account is None
    assert fake.calls == []
    assert utc_z(datetime(2026, 2, 2, 14, 30, tzinfo=UTC)) == "2026-02-02T14:30:00Z"


async def test_the_runner_token_goes_only_to_the_three_control_routes():
    seen: list[httpx.Request] = []
    fake = InMemoryMarket()
    client = httpx.AsyncClient(transport=delegating_transport(fake, seen), base_url="http://m")
    port = HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, TOKEN)
    result = await run_strategy(SPEC, port, buy_and_hold(port.price_at))

    assert result.state is RunState.COMPLETED
    routes = {route(r) for r in seen}
    assert CONTROL_ROUTES < routes and {"POST /orders", "GET /AAPL", "GET /portfolio"} < routes
    for request in seen:
        sent = request.headers.get(RUNNER_TOKEN_HEADER)
        assert sent == (TOKEN if route(request) in CONTROL_ROUTES else None), route(request)
        assert request.headers[APPROVAL_HEADER] == str(SPEC.approval_id)
    assert TOKEN not in result.model_dump_json()


async def test_a_wrong_token_fails_the_run_as_runner_unauthorized():
    # A missing token never leaves the adapter: the constructor and from_env refuse it.
    fake = InMemoryMarket()
    client = httpx.AsyncClient(transport=delegating_transport(fake), base_url="http://m")
    port = HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, "wrong-token")
    result = await run_strategy(SPEC, port, buy_and_hold(port.price_at))
    assert result.state is RunState.FAILED
    assert result.failure_code is ErrorCode.UNAUTHORIZED
    assert result.failure.startswith("RunnerUnauthorized: unauthorized: runner token required")
    assert fake.calls == []


@pytest.mark.parametrize(
    "call",
    [
        lambda port: port.set_cutoff(EID, OPEN, "synthetic-v1", "exec-v1"),
        lambda port: port.create_account(
            EID, SPEC.agent_id, SPEC.strategy_version_id, Decimal(1), request_id=SPEC.run_id
        ),
        lambda port: port.close_account(EID, ACCOUNT_ID),
    ],
)
async def test_401_on_a_control_route_is_runner_unauthorized(call):
    port, _ = port_answering(api_error(401, ErrorCode.UNAUTHORIZED))
    with pytest.raises(RunnerUnauthorized) as error:
        await call(port)
    assert not isinstance(error.value, ApprovalDenied | FutureData)


async def test_the_token_never_appears_in_an_error():
    port, _ = port_answering(api_error(409, ErrorCode.CONFLICT, f"bad header {TOKEN}"))
    with pytest.raises(MarketError) as error:
        await port.set_cutoff(EID, OPEN, "synthetic-v1", "exec-v1")
    assert TOKEN not in str(error.value) and TOKEN not in error.value.detail.message
    assert "[redacted]" in str(error.value)

    def echo(request):
        raise httpx.ConnectError(f"refused with {request.headers[RUNNER_TOKEN_HEADER]}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(echo), base_url="http://m")
    with pytest.raises(MarketError) as error:
        await HttpMarketPort(client, EID, APPROVAL, TOKEN).close_account(EID, ACCOUNT_ID)
    assert TOKEN not in str(error.value)
    assert TOKEN not in repr(HttpMarketPort(client, EID, APPROVAL, TOKEN))


def test_from_env_requires_the_token_at_startup(monkeypatch):
    client = httpx.AsyncClient(base_url="http://m")
    monkeypatch.delenv(RUNNER_TOKEN_ENV, raising=False)
    with pytest.raises(RunnerConfigError, match=RUNNER_TOKEN_ENV):
        HttpMarketPort.from_env(client, EID, APPROVAL)
    monkeypatch.setenv(RUNNER_TOKEN_ENV, "")
    with pytest.raises(RunnerConfigError):
        HttpMarketPort.from_env(client, EID, APPROVAL)
    monkeypatch.setenv(RUNNER_TOKEN_ENV, TOKEN)
    HttpMarketPort.from_env(client, EID, APPROVAL)
    with pytest.raises(RunnerConfigError):
        HttpMarketPort(client, EID, APPROVAL, "")
