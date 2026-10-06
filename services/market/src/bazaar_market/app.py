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
from fastapi import Depends, FastAPI

from bazaar_market import bundles, db, ledger_api, prices, prices_api
from bazaar_market.clock import SqliteClock
from bazaar_market.dev_grants import DevAllowListGrants
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
    # Approval allows and denials must also be visible in the server's own terminal.
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    market_logger.addHandler(console)


def create_app(
    database_path: Path | None = None,
    grants: GrantChecker | None = None,
    runner_token: str | None = None,
) -> FastAPI:
    path = database_path or Path(os.getenv("BAZAAR_MARKET_DB", DEFAULT_DB))
    token = runner_token if runner_token is not None else os.getenv("BAZAAR_RUNNER_TOKEN")
    grants = grants or DevAllowListGrants.from_env() or DenyAllGrants()
    clock = SqliteClock(path)

    def bars_for(data_version: str) -> prices.SqliteMarketData:
        """Prices for an experiment data_version: a bundle's bars, or a plain bars version."""
        with db.read_connection(path) as connection:
            return prices.SqliteMarketData(
                path, bundles.component(connection, data_version, "bars")
            )

    ledger = Ledger(path, bars_for)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.initialize(path, bundles.SCHEMA)
        ledger.initialize()
        with closing(prices.sqlite3.connect(path)) as connection:
            prices.ensure_schema(connection)
        with db.write_transaction(path) as connection:
            bundles.seed(connection)
        yield

    def market_data_for(experiment_id: UUID) -> prices.SqliteMarketData:
        return bars_for(clock.experiment(experiment_id).data_version)

    def component(experiment_id: UUID, kind: bundles.Kind) -> str:
        """The experiment's version of `kind`. Raises UnknownExperiment or bundles.NoComponent."""
        data_version = clock.experiment(experiment_id).data_version
        with db.read_connection(path) as connection:
            return bundles.component(connection, data_version, kind)

    configure_telemetry()
    app = FastAPI(title="Bazaar market", lifespan=lifespan)
    app.state.clock = clock
    app.state.ledger = ledger
    app.state.market_db_path = path
    app.state.market_data_for = market_data_for
    app.state.component = component

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(prices_api.router, dependencies=[Depends(ledger_api.approval_check(grants))])
    ledger_api.install(app, ledger, grants, token)
    logfire.instrument_fastapi(
        app,
        capture_headers=False,
        request_attributes_mapper=lambda request, attributes: None,
        excluded_urls="/health,/docs,/openapi.json",
    )
    return app


app = create_app()
