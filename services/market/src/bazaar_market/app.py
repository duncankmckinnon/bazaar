"""The market service: prices, the experiment clock, accounts and orders, in one SQLite file.

Run locally:
    BAZAAR_MARKET_DB=data/market.sqlite3 uv run uvicorn bazaar_market.app:app --port 8000
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from functools import cache
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import logfire
from fastapi import FastAPI

from bazaar_market import ledger_api, prices, prices_api
from bazaar_market.clock import SqliteClock
from bazaar_market.ledger import Ledger
from bazaar_market.ledger_api import DenyAllGrants, GrantChecker

DEFAULT_DB = Path("data/market.sqlite3")


@cache
def configure_telemetry() -> None:
    logfire.configure(
        send_to_logfire="if-token-present",
        service_name="bazaar-market",
        service_version=version("bazaar-market"),
        environment=os.getenv("BAZAAR_ENVIRONMENT", "development"),
        console=False,
        inspect_arguments=False,
        distributed_tracing=True,
    )
    market_logger = logging.getLogger("bazaar_market")
    market_logger.setLevel(logging.INFO)
    market_logger.addHandler(logfire.LogfireLoggingHandler())


def create_app(database_path: Path | None = None, grants: GrantChecker | None = None) -> FastAPI:
    path = database_path or Path(os.getenv("BAZAAR_MARKET_DB", DEFAULT_DB))
    clock = SqliteClock(path)
    ledger = Ledger(path, lambda data_version: prices.SqliteMarketData(path, data_version))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ledger.initialize()
        with closing(prices.sqlite3.connect(path)) as connection:
            prices.ensure_schema(connection)
        yield

    def market_data_for(experiment_id: UUID) -> prices.SqliteMarketData:
        return prices.SqliteMarketData(path, clock.experiment(experiment_id).data_version)

    configure_telemetry()
    app = FastAPI(title="Bazaar market", lifespan=lifespan)
    app.state.clock = clock
    app.state.market_data_for = market_data_for

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(prices_api.router)
    ledger_api.install(app, ledger, grants or DenyAllGrants())
    logfire.instrument_fastapi(
        app,
        capture_headers=False,
        request_attributes_mapper=lambda request, attributes: None,
        excluded_urls="/health,/docs,/openapi.json",
    )
    return app


app = create_app()
