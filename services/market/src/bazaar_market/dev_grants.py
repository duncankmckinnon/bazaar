"""DEV ONLY: approve listed (approval, experiment) pairs until the approval service (#18) exists.

This module and the one line in app.py that selects it are a single commit, meant to be dropped
before any PR. It is off unless BAZAAR_DEV_APPROVAL_IDS is set, and it logs a warning on every
call it allows. Each entry is `<approval_id>:<experiment_id>`, so an approval is good for one
experiment only.
"""

import logging
import os
from collections.abc import Mapping
from uuid import UUID

logger = logging.getLogger(__name__)

ENV_VAR = "BAZAAR_DEV_APPROVAL_IDS"


class DevAllowListGrants:
    def __init__(self, pairs: frozenset[tuple[UUID, UUID]]) -> None:
        self.pairs = pairs

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "DevAllowListGrants | None":
        """None when the variable is unset or empty. A malformed entry fails at startup."""
        entries = [part.strip() for part in environ.get(ENV_VAR, "").split(",") if part.strip()]
        if not entries:
            return None
        pairs = set()
        for entry in entries:
            approval, separator, experiment = entry.partition(":")
            if not separator:
                raise ValueError(f"{ENV_VAR} entries must be <approval_id>:<experiment_id>")
            pairs.add((UUID(approval), UUID(experiment)))
        return cls(frozenset(pairs))

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        if (approval_id, experiment_id) not in self.pairs:
            return False
        logger.warning(
            "DEV allow-list approved approval_id=%s experiment_id=%s; this is not a real approval",
            approval_id,
            experiment_id,
        )
        return True
