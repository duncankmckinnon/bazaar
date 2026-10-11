"""Monty's own spans for code-mode runs, through the process's Logfire configuration.

pydantic_monty.instrument_telemetry installs process-wide and raises if called again, and logfire
5.1.1 has no instrument_monty. So this installs it once, on the first code-mode run, with the
global OpenTelemetry tracer that logfire.configure sets (it follows later reconfiguration).
"""

import threading

import pydantic_monty
from opentelemetry import trace

_installed = False
_lock = threading.Lock()


def instrument_monty() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        try:
            pydantic_monty.instrument_telemetry(tracer=trace.get_tracer("pydantic_monty"))
        except RuntimeError:
            pass  # "Monty telemetry is already configured": someone else in this process did it
        _installed = True
