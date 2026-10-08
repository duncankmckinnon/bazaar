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
from typing import Any

import logfire
from logfire._internal.config import GLOBAL_CONFIG
from logfire._internal.scrubbing import DEFAULT_PATTERNS

# Logfire's defaults have no "token" pattern, so the runner and admin token headers are added; a
# bare "token" would also scrub gen_ai.usage.*_tokens. The approval header is the market
# capability, so it is scrubbed too even though the approval id is not a secret.
EXTRA_SCRUB_PATTERNS = (
    r"runner[._ -]?token",
    r"admin[._ -]?token",
    r"x[._ -]?bazaar[._ -]?approval",
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
    "LOGFIRE_TOKEN",
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


_configure_lock = threading.Lock()


def _service_version(service_name: str) -> str | None:
    try:
        return version(service_name)
    except PackageNotFoundError:
        return None


def configure(service_name: str, service_version: str | None = None) -> bool:
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
        logfire.configure(
            send_to_logfire="if-token-present",
            service_name=service_name,
            service_version=service_version or _service_version(service_name),
            environment=os.getenv("BAZAAR_ENVIRONMENT", "development"),
            console=False,
            inspect_arguments=False,
            distributed_tracing=True,
            scrubbing=scrubbing_options(),
        )
        return True
