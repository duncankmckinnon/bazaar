import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market.clock import Experiment, UnknownExperiment
from bazaar_market.prices import Bar, SqliteMarketData, close_at, import_bars
from bazaar_market.prices_api import router
from bazaar_protocol import PriceHistory
from fastapi import FastAPI
from fastapi.testclient import TestClient

EXPERIMENT = UUID("00000000-0000-4000-8000-000000000001")
MON, TUE, WED = date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)


class FakeClock:
    def __init__(self, cutoff: datetime, data_version: str = "synthetic-v1") -> None:
        self.at = cutoff
        self.data_version = data_version

    def experiment(self, experiment_id: UUID) -> Experiment:
        if experiment_id != EXPERIMENT:
            raise UnknownExperiment(str(experiment_id))
        return Experiment(experiment_id, self.data_version, "exec-v1", self.at, 1)


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        bars = [
            Bar("AAPL", day, Decimal(p), Decimal(p), Decimal(p), Decimal(p), 10)
            for day, p in ((MON, "100.00"), (TUE, "101.50"), (WED, "99.25"))
        ]
        import_bars(connection, bars, data_version="synthetic-v1", source="synthetic")
    application = FastAPI()
    application.include_router(router)
    application.state.clock = FakeClock(close_at(TUE))
    application.state.market_data_for = lambda _: SqliteMarketData(path, "synthetic-v1")
    return application


def get(app, start, end, symbol="AAPL", experiment=EXPERIMENT, **params):
    return TestClient(app).get(
        f"/experiments/{experiment}/prices/{symbol}",
        params={"start_at": start.isoformat(), "end_at": end.isoformat(), **params},
    )


def test_history_up_to_the_cutoff_includes_the_close_at_the_boundary_instant(app):
    response = get(app, close_at(MON), close_at(TUE))

    assert response.status_code == 200
    history = PriceHistory.model_validate(response.json())
    assert [str(o.price) for o in history.observations] == ["100.00", "101.50"]
    assert (history.cutoff_at, history.data_version, history.source) == (
        close_at(TUE),
        "synthetic-v1",
        "synthetic/synthetic-v1",
    )


def test_a_cutoff_one_microsecond_before_a_close_excludes_it(app):
    app.state.clock.at = close_at(TUE) - timedelta(microseconds=1)

    response = get(app, close_at(MON), app.state.clock.at)

    assert [o["price"] for o in response.json()["observations"]] == ["100.00"]


def test_an_end_after_the_cutoff_is_forbidden(app):
    response = get(app, close_at(MON), close_at(TUE) + timedelta(microseconds=1))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_an_unknown_symbol_is_not_found(app):
    response = get(app, close_at(MON), close_at(TUE), symbol="MSFT")

    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")


def test_an_unknown_experiment_is_not_found(app):
    response = get(app, close_at(MON), close_at(TUE), experiment=uuid4())

    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"start_at": "2026-02-02T21:00:00", "end_at": "2026-02-03T21:00:00Z"},
        {"start_at": "2026-02-03T21:00:00Z", "end_at": "2026-02-02T21:00:00Z"},
        {"start_at": "2026-02-02T21:00:00Z", "end_at": "2026-02-03T21:00:00Z", "limit": "0"},
    ],
)
def test_malformed_queries_are_rejected(app, params):
    response = TestClient(app).get(f"/experiments/{EXPERIMENT}/prices/AAPL", params=params)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")


def test_the_limit_caps_the_observations(app):
    response = get(app, close_at(MON), close_at(TUE), limit="1")

    assert len(response.json()["observations"]) == 1


def test_a_timestamp_that_is_not_utc_is_rejected_as_the_protocol_requires(app):
    response = TestClient(app).get(
        f"/experiments/{EXPERIMENT}/prices/AAPL",
        params={"start_at": close_at(MON).isoformat(), "end_at": "2026-02-03T16:00:00-05:00"},
    )

    assert response.status_code == 422


def test_a_bundle_experiment_reports_its_bundle_version_and_the_bars_as_the_source(app):
    app.state.clock.data_version = "demo-bundle-v1"

    response = get(app, close_at(MON), close_at(TUE))

    history = PriceHistory.model_validate(response.json())
    assert (history.data_version, history.source) == ("demo-bundle-v1", "synthetic/synthetic-v1")
    assert [str(o.price) for o in history.observations] == ["100.00", "101.50"]
