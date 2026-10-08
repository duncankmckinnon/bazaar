"""A model whose errors leave with configured secrets redacted, before any span records them.

pydantic-ai's model-request spans record a raised error's message and stacktrace, and Logfire
never scrubs exception text. Wrap a model in RedactingModel INSIDE any instrumentation, so the
error the span sees is already redacted. The redaction rules are bazaar_protocol.telemetry's.
"""

from typing import Any

from bazaar_protocol.telemetry import redacted_exceptions
from pydantic_ai.models.wrapper import WrapperModel


class RedactingModel(WrapperModel):
    """An error whose text holds a configured secret leaves as a RedactedError with the secret
    removed and no chain; any other error, and every response, is unchanged."""

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        with redacted_exceptions():
            return await self.wrapped.request(*args, **kwargs)
