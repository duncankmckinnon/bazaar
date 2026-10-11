"""Which trading role the agent runs with: the managed Logfire variable or the code default.

$BAZAAR_TRADING_ROLE_SOURCE is operator-only (never strategy text or a request):
- unset, empty or 'managed': the managed bazaar_trading_role variable, as before;
- 'code': bazaar_agent.trading.TRADING_ROLE, the reviewed code default, even when the managed
  value differs, through the variable's documented override (a context variable), so a refresh
  cannot bring a stale managed value back.
Any other value fails before anything else happens, without echoing it.
"""

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

ROLE_SOURCE_ENV = "BAZAAR_TRADING_ROLE_SOURCE"
ROLE_SOURCES = ("managed", "code")
# The source in effect for the runs started inside trading_role_from, for the runner.run span.
_active: ContextVar[str | None] = ContextVar("bazaar_trading_role_source", default=None)


class RoleSourceError(Exception):
    """$BAZAAR_TRADING_ROLE_SOURCE is set to something other than 'code' or 'managed'."""


def role_source(environ: Mapping[str, str] = os.environ) -> str:
    value = environ.get(ROLE_SOURCE_ENV, "")
    if not value:
        return "managed"
    if value not in ROLE_SOURCES:
        raise RoleSourceError(f"${ROLE_SOURCE_ENV} must be 'code' or 'managed'")
    return value


@contextmanager
def trading_role_from(source: str) -> Iterator[None]:
    """Run with `source`'s role. Enter it before asyncio.run, which copies this context."""
    if source not in ROLE_SOURCES:
        raise RoleSourceError(f"${ROLE_SOURCE_ENV} must be 'code' or 'managed'")
    token = _active.set(source)
    logger.info("trading role source: %s", source)
    try:
        if source == "code":
            from bazaar_agent.trading import TRADING_ROLE, TRADING_ROLE_VARIABLE

            with TRADING_ROLE_VARIABLE.override(TRADING_ROLE):
                yield
        else:
            yield
    finally:
        _active.reset(token)


def active_role_source() -> str | None:
    """The source set by the enclosing trading_role_from, if any (the name only)."""
    return _active.get()
