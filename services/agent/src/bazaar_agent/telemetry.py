"""Token-optional monitoring shared by the agent-side API and agent entry point."""

import os
from functools import cache
from importlib.metadata import version

import logfire


@cache
def configure_telemetry() -> None:
    name = os.getenv("AGENT_NAME", "api")
    logfire.configure(
        send_to_logfire="if-token-present",
        service_name=f"bazaar-agent-{name}",
        service_version=version("bazaar-agent"),
        environment=os.getenv("BAZAAR_ENVIRONMENT", "development"),
        console=False,
        inspect_arguments=False,
        distributed_tracing=True,
    )
    logfire.instrument_system_metrics()
