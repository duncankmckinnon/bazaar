"""Data bundles: one experiment-facing data_version naming a bars, news and filings version.

An experiment's data_version is either a bundle id or, for backward compatibility, a plain bars
version (alpaca-bars-v1, synthetic-v1), which has bars only. Pages and items report the
experiment's data_version; the component version goes in their source.
"""

import sqlite3
from typing import Literal

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_bundles (
    bundle_id TEXT PRIMARY KEY,
    bars TEXT NOT NULL,
    news TEXT NOT NULL,
    filings TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS data_bundles_immutable_update BEFORE UPDATE ON data_bundles
BEGIN SELECT RAISE(ABORT, 'bundles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS data_bundles_immutable_delete BEFORE DELETE ON data_bundles
BEGIN SELECT RAISE(ABORT, 'bundles are immutable'); END;
"""

Kind = Literal["bars", "news", "filings"]

DEMO_BUNDLES: dict[str, dict[Kind, str]] = {
    "demo-bundle-v1": {
        "bars": "alpaca-bars-v1",
        "news": "alpaca-news-v1",
        "filings": "edgar-filings-v1",
    },
}


class NoComponent(LookupError):
    """The experiment's data_version has no version of this kind. Treat as missing coverage."""


class BundleConflict(Exception):
    """A bundle id is already stored with different components."""


def seed(
    connection: sqlite3.Connection, bundles: dict[str, dict[Kind, str]] = DEMO_BUNDLES
) -> None:
    """Store `bundles`. Re-seeding the same bundles is a no-op; a changed bundle is refused."""
    for bundle_id, parts in bundles.items():
        connection.execute(
            "INSERT OR IGNORE INTO data_bundles VALUES (?, ?, ?, ?)",
            (bundle_id, parts["bars"], parts["news"], parts["filings"]),
        )
        row = connection.execute(
            "SELECT bars, news, filings FROM data_bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        if tuple(row) != (parts["bars"], parts["news"], parts["filings"]):
            raise BundleConflict(f"{bundle_id} is already stored with different components")


def component(connection: sqlite3.Connection, data_version: str, kind: Kind) -> str:
    if kind not in ("bars", "news", "filings"):
        raise ValueError(f"unknown component kind {kind!r}")
    row = connection.execute(
        "SELECT bars, news, filings FROM data_bundles WHERE bundle_id = ?", (data_version,)
    ).fetchone()
    if row is not None:
        return dict(zip(("bars", "news", "filings"), row, strict=True))[kind]
    if kind == "bars":
        return data_version
    raise NoComponent(f"{data_version} has no {kind} version")
