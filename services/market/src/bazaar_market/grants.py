"""Approval grants recorded by the runner: each approval is good for exactly one experiment.

POST /control/grants needs the runner token and nothing else. An agent's approval header cannot
reach it. A grant is never changed or removed here: the same pair again is a no-op, and binding
an approval to a different experiment is refused.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from bazaar_protocol import ErrorCode, WireModel
from fastapi import APIRouter, Depends, Response

from bazaar_market import db
from bazaar_market.db import MarketError
from bazaar_market.ledger_api import runner_token_check

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS acct_grants (
    approval_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS acct_grants_immutable_update BEFORE UPDATE ON acct_grants
BEGIN SELECT RAISE(ABORT, 'grants are immutable'); END;
CREATE TRIGGER IF NOT EXISTS acct_grants_immutable_delete BEFORE DELETE ON acct_grants
BEGIN SELECT RAISE(ABORT, 'grants are immutable'); END;
"""


class GrantRequest(WireModel):
    approval_id: UUID
    experiment_id: UUID


class SqliteGrants:
    """A GrantChecker backed by acct_grants. No row means no approval."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        with db.read_connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT experiment_id FROM acct_grants WHERE approval_id = ?", (str(approval_id),)
            ).fetchone()
        return row is not None and row["experiment_id"] == str(experiment_id)

    def grant(self, approval_id: UUID, experiment_id: UUID) -> bool:
        """Record the grant. False if it already existed; 409 if the approval is bound elsewhere."""
        with db.write_transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT experiment_id FROM acct_grants WHERE approval_id = ?", (str(approval_id),)
            ).fetchone()
            if row is not None:
                if row["experiment_id"] != str(experiment_id):
                    logger.warning(
                        "grant refused: approval_id=%s is bound to another experiment, "
                        "not experiment_id=%s",
                        approval_id,
                        experiment_id,
                    )
                    raise MarketError(
                        409,
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "This approval is already granted for another experiment",
                    )
                return False
            connection.execute(
                "INSERT INTO acct_grants VALUES (?, ?, ?)",
                (str(approval_id), str(experiment_id), db.format_time(datetime.now(UTC))),
            )
        logger.info("grant created: approval_id=%s experiment_id=%s", approval_id, experiment_id)
        return True


def build_router(grants: SqliteGrants, runner_token: str | None) -> APIRouter:
    router = APIRouter(dependencies=[Depends(runner_token_check(runner_token))])

    @router.post("/control/grants", status_code=204)
    def create_grant(body: GrantRequest) -> Response:
        grants.grant(body.approval_id, body.experiment_id)
        return Response(status_code=204)

    return router
