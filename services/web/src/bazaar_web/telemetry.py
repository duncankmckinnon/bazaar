"""Logfire for the web process, which owns its telemetry as service "bazaar-web"."""

import os
from functools import cache
from importlib.metadata import version

import logfire

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
    """Configure Logfire once per process (sends only when a token is present)."""
    try:
        from bazaar_protocol.telemetry import configure as shared  # T0, once it lands
    except ImportError:
        shared = None
    if shared is not None:
        shared(SERVICE_NAME)
    else:
        # Fallback with T0's parameters; delete it when bazaar_protocol.telemetry exists.
        logfire.configure(
            send_to_logfire="if-token-present",
            service_name=SERVICE_NAME,
            service_version=version("bazaar-web"),
            environment=os.getenv("BAZAAR_ENVIRONMENT", "development"),
            distributed_tracing=True,
            console=False,
            scrubbing=logfire.ScrubbingOptions(
                extra_patterns=[r"runner[._ -]?token", r"admin[._ -]?token"]
            ),
        )
    logfire.instrument_system_metrics()
