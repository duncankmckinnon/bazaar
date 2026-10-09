"""Token-optional Logfire for the runner process. The library opens spans; entry points configure.

The configuration, scrubbing and secret redaction are shared by every service
(bazaar_protocol.telemetry).
"""

from bazaar_protocol import telemetry


def configure_telemetry() -> None:
    # A no-op when the process is already configured, e.g. by the web app that runs submissions.
    telemetry.configure("bazaar-runner", managed_variables=True)
