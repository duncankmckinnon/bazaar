"""Token-optional Logfire for the runner process. The library opens spans; the CLI configures."""

import os
from functools import cache
from importlib.metadata import version

import logfire


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
    )
