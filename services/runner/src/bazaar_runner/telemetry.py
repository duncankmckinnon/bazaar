"""Token-optional Logfire for the runner process. The library opens spans; the CLI configures."""

import os
import re
from functools import cache
from importlib.metadata import version
from typing import Any

import logfire
from logfire._internal.scrubbing import DEFAULT_PATTERNS

# Logfire scrubs any value or key matching its default patterns. "session" is everyday trading
# language ("trading session"), so a value whose only match is "session" is kept. Logfire hands
# the callback only the FIRST match, so the whole value and its key path are searched again for
# every other default pattern; any hit keeps it scrubbed. Scrubbing is never turned off.
_OTHER_SECRET_PATTERNS = re.compile(
    "|".join(p for p in DEFAULT_PATTERNS if p != "session"), re.IGNORECASE
)


def keep_trading_sessions(match: logfire.ScrubMatch) -> Any:
    if match.pattern_match.group(0).lower() != "session":
        return None
    text = " ".join(map(str, match.path)) + " " + str(match.value)
    return None if _OTHER_SECRET_PATTERNS.search(text) else match.value


@cache
def configure_telemetry() -> None:
    # Any write token comes from LOGFIRE_TOKEN in the environment; the runner never reads it.
    logfire.configure(
        send_to_logfire="if-token-present",
        service_name="bazaar-runner",
        service_version=version("bazaar-runner"),
        environment=os.getenv("BAZAAR_ENVIRONMENT", "development"),
        console=False,
        inspect_arguments=False,
        distributed_tracing=True,
        scrubbing=logfire.ScrubbingOptions(callback=keep_trading_sessions),
    )
