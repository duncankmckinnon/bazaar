"""Logfire for every Bazaar process: one configuration, one scrubbing rule, redacted exceptions.

`configure(service_name)` configures Logfire once per process and never overrides a
configuration that is already in place, whether it came from here or from a direct
`logfire.configure` call. Library code does not configure; process entry points do.

Logfire never scrubs `exception.message` or `exception.stacktrace` (they are safe keys), and its
patterns match secret names, not secret values. So exception text that crosses a span boundary
goes through `redact` / `redacted_exceptions`, which remove the configured secret values.
"""

import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, NoReturn

import httpx
import logfire
from logfire._internal.config import GLOBAL_CONFIG
from logfire._internal.scrubbing import DEFAULT_PATTERNS

# Logfire's defaults have no "token" pattern, so the runner and admin token headers are added; a
# bare "token" would also scrub gen_ai.usage.*_tokens. The approval header is the market
# capability, so it is scrubbed too even though the approval id is not a secret.
EMAIL_PATTERN = r"(?<=[\w.+-])@[\w-]+(?:\.[\w-]+)+"
EMAIL_SCRUBBED = "[Scrubbed email]"
EXTRA_SCRUB_PATTERNS = (
    r"runner[._ -]?token",
    r"admin[._ -]?token",
    # The strategist's Logfire query token (LOGFIRE_READ_TOKEN, logfire_read_token, read-token).
    r"read[._ -]?token",
    r"x[._ -]?bazaar[._ -]?approval",
    # Email addresses (submitters' contact details), as defence in depth: the web app never puts
    # them in a span. Logfire matches patterns against attribute names AND string values, so any
    # value holding an email-like string (news text, an agent message) is scrubbed too; that
    # over-redaction is accepted. It needs an "@" and a dotted domain, so no attribute name such
    # as gen_ai.usage.input_tokens can match. It matches from the "@" on: Logfire keeps a value
    # that matches as a WHOLE, so a pattern spanning a bare address would never scrub it.
    EMAIL_PATTERN,
)

# Logfire scrubs any value or key matching a pattern. "session" is everyday trading language
# ("trading session"), so a value whose only match is "session" is kept. Logfire hands the
# callback only the FIRST match, so the whole value and its key path are searched again for every
# other pattern; any hit keeps it scrubbed. Scrubbing is never turned off.
_OTHER_SECRET_PATTERNS = re.compile(
    "|".join([*(p for p in DEFAULT_PATTERNS if p != "session"), *EXTRA_SCRUB_PATTERNS]),
    re.IGNORECASE,
)


def keep_trading_sessions(match: logfire.ScrubMatch) -> Any:
    """Keep a value whose only match is "session"; replace an email-led one with EMAIL_SCRUBBED.

    Logfire's own replacement, "[Scrubbed due to '<match>']", would copy the address into the
    span (and its logfire.scrubbed note), so an email match gets a neutral value instead.
    """
    if match.pattern_match.group(0).startswith("@"):
        return EMAIL_SCRUBBED
    if match.pattern_match.group(0).lower() != "session":
        return None
    text = " ".join(map(str, match.path)) + " " + str(match.value)
    return None if _OTHER_SECRET_PATTERNS.search(text) else match.value


def scrubbing_options() -> logfire.ScrubbingOptions:
    return logfire.ScrubbingOptions(
        callback=keep_trading_sessions, extra_patterns=list(EXTRA_SCRUB_PATTERNS)
    )


# The secrets a Bazaar process may hold. Their values are read once, at configure, and kept only
# in this process's memory.
SECRET_ENV = (
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "BAZAAR_RUNNER_TOKEN",
    "BAZAAR_ADMIN_TOKEN",
    "LOGFIRE_API_KEY",
    "LOGFIRE_TOKEN",
    "LOGFIRE_READ_TOKEN",
    "LOGFIRE_API_WRITE_KEY",  # the strategist eval's dataset push (bazaar_agent.strategist_eval)
)
REDACTED = "[REDACTED]"
# Shorter values are not credentials and would redact ordinary text.
MIN_SECRET_LENGTH = 8
# "api_key=…", "Authorization: Bearer …", 'runner_token="…"', '"api_key": "…"': keep the name and
# any opening quote, drop the value.
_NAMED_VALUE = re.compile(
    rf"(?<![a-z])(?P<name>(?:{_OTHER_SECRET_PATTERNS.pattern})[\w.-]*)(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>(?:bearer\s+)?[^\s,;&'\"]+)",
    re.IGNORECASE,
)

_secrets: tuple[str, ...] | None = None
_lock = threading.Lock()


def _load_secrets() -> None:
    global _secrets
    with _lock:
        if _secrets is None:
            values = (os.environ.get(name, "") for name in SECRET_ENV)
            _secrets = tuple(v for v in values if len(v) >= MIN_SECRET_LENGTH)


def redact(text: str, *extra_secrets: str | None) -> str:
    """`text` without any configured secret value, `extra_secrets`, or a value named like one."""
    if _secrets is None:
        _load_secrets()
    secrets = {s for s in (*(_secrets or ()), *extra_secrets) if s and len(s) >= MIN_SECRET_LENGTH}
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _NAMED_VALUE.sub(lambda m: f"{m['name']}{m['sep']}{m['quote']}{REDACTED}", text)


class RedactedError(Exception):
    """An exception whose text held a secret: the message is redacted and its chain dropped."""


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exceptions a traceback prints: a context hidden by `raise ... from None` is not."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            current = current.__cause__
        else:
            current = None if current.__suppress_context__ else current.__context__


@contextmanager
def redacted_exceptions(*extra_secrets: str | None) -> Iterator[None]:
    """Put inside a span: an exception whose text, or whose chain's text, holds a secret leaves
    as a RedactedError raised from None, so the span records neither the secret nor the chain.
    Any other exception passes through unchanged, keeping its type for the caller."""
    try:
        yield
    except Exception as exc:
        if all(redact(str(e), *extra_secrets) == str(e) for e in _chain(exc)):
            raise
        message = redact(f"{type(exc).__name__}: {exc}", *extra_secrets)
    else:
        return
    # Raised outside the except block, and its context cleared as it leaves: the secret-bearing
    # exception is not even referenced from the error a span records.
    error = RedactedError(message)
    try:
        raise error from None
    finally:
        error.__context__ = None


def _transport_error(exc: Exception, request: httpx.Request) -> Exception | None:
    """A redacted copy of a transport error that holds a secret (None when it holds none).
    httpx errors keep their type, so callers that catch httpx.TransportError still do."""
    if all(redact(str(e)) == str(e) for e in _chain(exc)):
        return None
    if isinstance(exc, httpx.RequestError):
        return type(exc)(redact(str(exc)), request=request)
    return RedactedError(redact(f"{type(exc).__name__}: {exc}"))


def _raise_clean(error: Exception) -> NoReturn:
    try:
        raise error from None
    finally:
        error.__context__ = None


class _RedactingTransport(httpx.AsyncBaseTransport, httpx.BaseTransport):
    """Inside the client's HTTP span: an error the span records is already redacted."""

    def __init__(self, inner: httpx.AsyncBaseTransport | httpx.BaseTransport) -> None:
        self.inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return self.inner.handle_request(request)  # type: ignore[union-attr]
        except Exception as exc:
            error = _transport_error(exc, request)
            if error is None:
                raise
        _raise_clean(error)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self.inner.handle_async_request(request)  # type: ignore[union-attr]
        except Exception as exc:
            error = _transport_error(exc, request)
            if error is None:
                raise
        _raise_clean(error)

    def close(self) -> None:
        self.inner.close()  # type: ignore[union-attr]

    async def aclose(self) -> None:
        await self.inner.aclose()  # type: ignore[union-attr]


def instrument_market_client[C: httpx.AsyncClient | httpx.Client](client: C) -> C:
    """Trace this client's market requests and send traceparent, so the market's request spans
    join the caller's trace. Never headers or bodies (the runner token travels in a header).

    Per client, never process-wide: the Gateway's own client already propagates. The client's
    transport is first wrapped in its own redacting transport, so (1) the HTTP span records a
    transport error only after redaction and (2) a transport shared by several clients (tests
    pass one MockTransport around) is never instrumented more than once.
    """
    client._transport = _RedactingTransport(client._transport)
    logfire.instrument_httpx(
        client,
        capture_all=False,
        capture_headers=False,
        capture_request_body=False,
        capture_response_body=False,
    )
    return client


_configure_lock = threading.Lock()


def _service_version(service_name: str) -> str | None:
    try:
        return version(service_name)
    except PackageNotFoundError:
        return None


def configure(
    service_name: str,
    service_version: str | None = None,
    *,
    managed_variables: bool = False,
) -> bool:
    """Configure Logfire for this process unless it already is; True if this call configured it.

    Logfire marks its global configuration initialized only inside `logfire.configure`, so a
    process configured by anyone else (the web app, a test) keeps that configuration and name.
    The secret values for `redact` are read either way.
    """
    _load_secrets()
    with _configure_lock:
        # Only logfire.configure sets this (checked against logfire 5.1.1's source).
        if GLOBAL_CONFIG._initialized:
            return False
        # Any write token comes from LOGFIRE_TOKEN in the environment.
        variables = (
            {"variables": logfire.VariablesOptions(timeout=(2, 2))}
            if managed_variables and os.getenv("LOGFIRE_API_KEY")
            else {}
        )
        logfire.configure(
            send_to_logfire="if-token-present",
            service_name=service_name,
            service_version=service_version or _service_version(service_name),
            environment=os.getenv("BAZAAR_ENVIRONMENT", "development"),
            console=False,
            inspect_arguments=False,
            distributed_tracing=True,
            scrubbing=scrubbing_options(),
            **variables,
        )
        return True
