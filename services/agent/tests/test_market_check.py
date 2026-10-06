import httpx
from bazaar_agent.__main__ import market_is_healthy


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://market")


async def test_healthy_when_market_returns_200():
    async with _client(lambda r: httpx.Response(200, json={"status": "ok"})) as c:
        assert await market_is_healthy(c) is True


async def test_unhealthy_when_market_unreachable():
    def refuse(request):
        raise httpx.ConnectError("refused")

    async with _client(refuse) as c:
        assert await market_is_healthy(c) is False
