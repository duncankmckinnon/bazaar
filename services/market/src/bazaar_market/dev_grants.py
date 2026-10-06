"""DEV ONLY: approve a fixed list of approval ids until the approval service (#18) exists.

This module and the one line in app.py that selects it are a single commit, meant to be dropped
before any PR. It is off unless BAZAAR_DEV_APPROVAL_IDS is set, and it logs a warning on every
call it allows.
"""

import logging
import os
from collections.abc import Mapping
from uuid import UUID

logger = logging.getLogger(__name__)

ENV_VAR = "BAZAAR_DEV_APPROVAL_IDS"


class DevAllowListGrants:
    def __init__(self, approval_ids: frozenset[UUID]) -> None:
        self.approval_ids = approval_ids

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "DevAllowListGrants | None":
        """None when the variable is unset or empty. A malformed id fails at startup."""
        ids = [part.strip() for part in environ.get(ENV_VAR, "").split(",") if part.strip()]
        if not ids:
            return None
        return cls(frozenset(UUID(part) for part in ids))

    def allows(self, approval_id: UUID) -> bool:
        if approval_id not in self.approval_ids:
            return False
        logger.warning(
            "DEV allow-list approved approval_id=%s; this is not a real approval", approval_id
        )
        return True
