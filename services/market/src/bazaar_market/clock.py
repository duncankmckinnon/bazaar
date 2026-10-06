"""The trusted simulated clock. The runner moves it; every read and order is cut off by it."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from bazaar_protocol import ErrorCode

from bazaar_market import db


class UnknownExperiment(LookupError):
    """No clock exists for this experiment. Callers must not fall back to wall-clock time."""


class Clock(Protocol):
    def cutoff(self, experiment_id: UUID) -> datetime: ...


@dataclass(frozen=True)
class Experiment:
    experiment_id: UUID
    data_version: str
    execution_rule_version: str
    cutoff_at: datetime
    cutoff_seq: int


def load_experiment(connection: sqlite3.Connection, experiment_id: UUID) -> Experiment:
    row = connection.execute(
        "SELECT * FROM acct_experiments WHERE experiment_id = ?", (str(experiment_id),)
    ).fetchone()
    if row is None:
        raise UnknownExperiment(str(experiment_id))
    return Experiment(
        experiment_id=experiment_id,
        data_version=row["data_version"],
        execution_rule_version=row["execution_rule_version"],
        cutoff_at=db.parse_time(row["cutoff_at"]),
        cutoff_seq=row["cutoff_seq"],
    )


class SqliteClock:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def cutoff(self, experiment_id: UUID) -> datetime:
        with db.read_connection(self.database_path) as connection:
            return load_experiment(connection, experiment_id).cutoff_at

    def set_cutoff(
        self,
        experiment_id: UUID,
        cutoff: datetime,
        data_version: str | None = None,
        execution_rule_version: str | None = None,
    ) -> Experiment:
        """Create the clock on the first call, then only move it forward.

        The first call must name both versions. A later call may repeat them but not change them.
        An equal cutoff is a no-op; an earlier one is refused.
        """
        stored_cutoff = db.format_time(cutoff)
        with db.write_transaction(self.database_path) as connection:
            try:
                current = load_experiment(connection, experiment_id)
            except UnknownExperiment:
                if data_version is None or execution_rule_version is None:
                    raise db.MarketError(
                        422,
                        ErrorCode.INVALID_REQUEST,
                        "The first cutoff must name data_version and execution_rule_version",
                    ) from None
                connection.execute(
                    "INSERT INTO acct_experiments VALUES (?, ?, ?, ?, 1)",
                    (str(experiment_id), data_version, execution_rule_version, stored_cutoff),
                )
                return load_experiment(connection, experiment_id)
            if (data_version is not None and data_version != current.data_version) or (
                execution_rule_version is not None
                and execution_rule_version != current.execution_rule_version
            ):
                raise db.MarketError(
                    409, ErrorCode.INVALID_REQUEST, "Experiment versions cannot change"
                )
            if cutoff == current.cutoff_at:
                return current
            if cutoff < current.cutoff_at:
                raise db.MarketError(
                    409, ErrorCode.INVALID_REQUEST, "The cutoff cannot move backwards"
                )
            connection.execute(
                "UPDATE acct_experiments SET cutoff_at = ?, cutoff_seq = cutoff_seq + 1 "
                "WHERE experiment_id = ?",
                (stored_cutoff, str(experiment_id)),
            )
            return load_experiment(connection, experiment_id)
