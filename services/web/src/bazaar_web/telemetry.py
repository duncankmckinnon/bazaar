"""Logfire for the web process, which owns its telemetry as service "bazaar-web"."""

from functools import cache

import logfire
from bazaar_protocol import telemetry as shared

SERVICE_NAME = "bazaar-web"
# Requests the board and form poll every few seconds; tracing them floods Logfire. Patterns are
# regexes searched in the full URL and cannot see the method, so the status poll is anchored to
# an id segment: POST /api/submissions and /api/admin/submissions/{id}/hide stay traced.
EXCLUDED_URLS = (
    r"/api/board(\?|$)",
    r"/api/submissions/[^/?]+(\?|$)",
    r"/fonts/",
    r"/static/",
    r"/health(\?|$)",
)


@cache
def configure() -> None:
    """Configure Logfire once per process as bazaar-web, with the shared scrubbing (T0)."""
    shared.configure(SERVICE_NAME)
    logfire.instrument_system_metrics()
