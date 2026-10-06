import asyncio
import logging
import os

import httpx

log = logging.getLogger("bazaar_agent")


async def market_is_healthy(client: httpx.AsyncClient) -> bool:
    try:
        return (await client.get("/health")).status_code == 200
    except httpx.TransportError:
        return False


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    name = os.environ.get("AGENT_NAME", "agent")
    interval = float(os.environ.get("POLL_INTERVAL", "5"))
    async with httpx.AsyncClient(
        base_url=os.environ.get("MARKET_URL", "http://market:8000"), timeout=5
    ) as client:
        while True:
            healthy = await market_is_healthy(client)
            log.info("%s: market %s", name, "reachable" if healthy else "unreachable")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
