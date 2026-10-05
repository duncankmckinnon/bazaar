from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Deal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    market_price REAL NOT NULL,
    rounds INTEGER NOT NULL,
    closed_at TEXT NOT NULL
)
"""


class Ledger:
    """Historical transaction store that agents and dashboards read from."""

    def __init__(self, path: str | Path = ":memory:"):
        self._db = sqlite3.connect(str(path))
        self._db.execute(_SCHEMA)

    def record(self, deal: Deal) -> None:
        self._db.execute(
            "INSERT INTO deals (symbol, quantity, price, market_price, rounds, closed_at) VALUES (?,?,?,?,?,?)",
            (
                deal.commodity.symbol,
                deal.quantity,
                deal.price,
                deal.market_price,
                deal.rounds,
                deal.closed_at.isoformat(),
            ),
        )
        self._db.commit()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self._db.execute(sql, params).fetchall()
