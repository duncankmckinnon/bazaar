import sqlite3

import pytest
from bazaar_market import bundles, db


@pytest.fixture
def connection(tmp_path):
    db.initialize(tmp_path / "m.db", bundles.SCHEMA)
    with db.write_transaction(tmp_path / "m.db") as connection:
        yield connection


def test_the_demo_bundle_resolves_each_component(connection):
    bundles.seed(connection)
    bundles.seed(connection)
    assert bundles.component(connection, "demo-bundle-v1", "bars") == "alpaca-bars-v1"
    assert bundles.component(connection, "demo-bundle-v1", "news") == "alpaca-news-v1"
    assert bundles.component(connection, "demo-bundle-v1", "filings") == "edgar-filings-v1"


def test_a_plain_bars_version_has_bars_only(connection):
    bundles.seed(connection)
    assert bundles.component(connection, "synthetic-v1", "bars") == "synthetic-v1"
    with pytest.raises(bundles.NoComponent):
        bundles.component(connection, "synthetic-v1", "news")


def test_an_unknown_kind_is_refused(connection):
    with pytest.raises(ValueError):
        bundles.component(connection, "demo-bundle-v1", "bars FROM data_bundles --")


def test_a_bundle_cannot_change(connection):
    bundles.seed(connection)
    changed = {"demo-bundle-v1": {"bars": "x", "news": "alpaca-news-v1", "filings": "f"}}
    with pytest.raises(bundles.BundleConflict):
        bundles.seed(connection, changed)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE data_bundles SET bars = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM data_bundles")
