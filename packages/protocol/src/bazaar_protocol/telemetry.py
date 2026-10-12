"""Logfire for every Bazaar process: one configuration, one scrubbing rule, redacted exceptions.

`configure(service_name)` configures Logfire once per process and never overrides a
configuration that is already in place, whether it came from here or from a direct
`logfire.configure` call. Library code does not configure; process entry points do.

Span scrubbing replaces only real secrets: the configured secret values, emails and
credential keys' values (see precise_scrub); free text is never scrubbed for a word. Logfire never
scrubs `exception.message` or `exception.stacktrace` (they are safe keys), so exception text that
crosses a span boundary goes through `redact` / `redacted_exceptions`, which remove the
configured secret values.
"""

import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, NoReturn

import httpx
import logfire
from logfire._internal.config import GLOBAL_CONFIG
from logfire._internal.scrubbing import DEFAULT_PATTERNS

# Span scrubbing (SC1): only real secrets, never free text by a word match.
# Logfire always applies its DEFAULT_PATTERNS ("auth", "secret", "session", "cookie"...) to keys
# and string values, and hands the FIRST match to the callback. Our patterns only add triggers:
# each loaded secret value, emails, and credential-shaped keys. precise_scrub then decides:
# a secret value or an email is replaced, a credential key's value is replaced, and anything
# else (role text, news, "market-authoritative", "trading session", cache_read_tokens) is kept.
EMAIL_PATTERN = r"(?<=[\w.+-])@[\w-]+(?:\.[\w-]+)+"
EMAIL_SCRUBBED = "[Scrubbed email]"
_EMAIL = re.compile(EMAIL_PATTERN)
# Key triggers only (the callback keeps a value that is not under a credential key): header and
# attribute names such as x-bazaar-runner-token or read_token, never the counts *_tokens.
_KEY_TRIGGERS = (r"token\b", r"x[._ -]?bazaar[._ -]?approval")
# A secret value this short is not used as a pattern (it could match ordinary text).
MIN_SCRUB_SECRET_LENGTH = 16
_CREDENTIAL_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-bazaar-approval",
        "api_key",
        "apikey",
        "api-key",
        "password",
        "secret",
    }
)
_BAZAAR_TOKEN_HEADER = re.compile(r"x-bazaar-[\w-]+-token")
# A key NAME containing one of these is a credential key (client_secret, private_key, jwt,
# apca-api-secret-key, authorization-foreign). Keys only: values are never matched by a word.
_CREDENTIAL_KEY_PARTS = re.compile(
    r"secret|password|passwd|credential|private[._ -]?key|jwt|authorization"
)


def _credential_key(key: object) -> bool:
    """A key whose value is a credential: checked as a whole and after its last '.', so
    http.request.header.authorization and logfire.token count. A *_tokens count never does."""
    whole = str(key).lower()
    for name in {whole, whole.rsplit(".", 1)[-1]}:
        if name in _CREDENTIAL_KEYS or _BAZAAR_TOKEN_HEADER.fullmatch(name):
            return True
        # token, logfire.token, read_token, refresh-token; never input_tokens or *_tokens.
        if name.endswith("token") or _CREDENTIAL_KEY_PARTS.search(name):
            return True
    return False


def _holds_secret(path: tuple, value: object) -> bool:
    secrets = _secrets or ()
    texts = [str(value), *map(str, path)]
    return any(secret in text for secret in secrets for text in texts)


def _precise(path: tuple, value: Any) -> Any:
    """The value to export: a neutral marker for a secret, an email or a credential key's value;
    otherwise the value itself (a mapping or sequence is checked item by item)."""
    if _holds_secret(path, value):
        # Never None: Logfire's default replacement quotes the match, which could be the secret.
        return REDACTED
    if path and _credential_key(path[-1]):
        # Header values come as sequences; keep the shape.
        return type(value)(REDACTED for _ in value) if isinstance(value, list | tuple) else REDACTED
    if isinstance(value, Mapping):
        return {k: _precise((*path, k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_precise((*path, i), v) for i, v in enumerate(value))
    if isinstance(value, str) and _EMAIL.search(value):
        return EMAIL_SCRUBBED
    return value


def precise_scrub(match: logfire.ScrubMatch) -> Any:
    """Logfire's scrubbing callback: replace only secrets, emails and credential keys' values.

    It never returns None, so Logfire's own "[Scrubbed due to '<match>']" (which quotes the
    match) is never used."""
    return _precise(tuple(match.path), match.value)


def _secret_pattern(secret: str) -> str:
    # Logfire keeps a value its pattern matches as a WHOLE, so a value that is exactly the secret
    # must not be a whole match: match all but the last character and look ahead for it.
    return re.escape(secret[:-1]) + "(?=" + re.escape(secret[-1]) + ")"


def scrubbing_options() -> logfire.ScrubbingOptions:
    """The scrubbing rule, with the secret values loaded now (configure calls this)."""
    _load_secrets()
    secrets = sorted(
        {s for s in (_secrets or ()) if len(s) >= MIN_SCRUB_SECRET_LENGTH}, key=len, reverse=True
    )
    return logfire.ScrubbingOptions(
        callback=precise_scrub,
        extra_patterns=[EMAIL_PATTERN, *_KEY_TRIGGERS, *map(_secret_pattern, secrets)],
    )


# For redact() only (exception text, a separate path): a value NAMED like a secret
# ("api_key=…", "runner_token: …") is dropped there. Span scrubbing does not use these words.
_REDACT_NAME_PATTERNS = (
    r"runner[._ -]?token",
    r"admin[._ -]?token",
    r"read[._ -]?token",
    r"x[._ -]?bazaar[._ -]?approval",
    EMAIL_PATTERN,
)
_OTHER_SECRET_PATTERNS = re.compile(
    "|".join([*(p for p in DEFAULT_PATTERNS if p != "session"), *_REDACT_NAME_PATTERNS]),
    re.IGNORECASE,
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
    "BAZAAR_LOGFIRE_BROWSER_TOKEN",
    "PAIG_API_KEY",
    "BAZAAR_CURSOR_SECRET",
)
REDACTED = "[REDACTED]"
STRATEGY_NAME_BAGGAGE = "bazaar.strategy_name"
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


def strategy_label(fallback: str = "unnamed") -> str:
    """The run's strategy name from Logfire baggage (record_run sets it), for a span's message.

    It is attendee text: pass it as an attribute value formatted by a fixed template
    ("{strategy} · ..."), never inside the template itself."""
    value = logfire.get_baggage().get(STRATEGY_NAME_BAGGAGE)
    return str(value) if value else fallback
