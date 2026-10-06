import asyncio
import logging
import os

import httpx
import logfire

from bazaar_agent.telemetry import configure_telemetry

log = logging.getLogger("bazaar_agent")


async def market_is_healthy(client: httpx.AsyncClient) -> bool:
    try:
        return (await client.get("/health")).status_code == 200
    except httpx.TransportError:
        return False


async def main() -> None:
    configure_telemetry()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logfire.LogfireLoggingHandler()],
    )
    name = os.environ.get("AGENT_NAME", "agent")
    interval = float(os.environ.get("POLL_INTERVAL", "5"))
    async with httpx.AsyncClient(
        base_url=os.environ.get("MARKET_URL", "http://market:8000"), timeout=5
    ) as client:
        logfire.instrument_httpx(
            client,
            capture_all=False,
            capture_headers=False,
            capture_request_body=False,
            capture_response_body=False,
        )
        while True:
            healthy = await market_is_healthy(client)
            log.info("%s: market %s", name, "reachable" if healthy else "unreachable")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
