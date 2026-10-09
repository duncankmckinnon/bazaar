"""Shared Logfire setup: configure once, scrub trading sessions but not secrets, redact values."""

import logfire
import pytest
from bazaar_protocol import telemetry
from bazaar_protocol.telemetry import (
    EXTRA_SCRUB_PATTERNS,
    REDACTED,
    RedactedError,
    keep_trading_sessions,
    redact,
    redacted_exceptions,
    scrubbing_options,
)
from logfire._internal.config import GLOBAL_CONFIG
from logfire._internal.scrubbing import Scrubber

SENTINEL = "sentinel-runner-token-9a8b"
GATEWAY_KEY = "sentinel-gateway-key-0f1e"
VARIABLES_KEY = "sentinel-variables-key-6d2c"


def scrub(value, key: str = "message"):
    scrubber = Scrubber(EXTRA_SCRUB_PATTERNS, keep_trading_sessions)
    scrubbed, _ = scrubber.scrub_value(("attributes", key), value)
    return scrubbed


def scrub_attributes(attributes: dict) -> dict:
    """As Logfire scrubs a span: attribute names are checked only inside the mapping."""
    scrubbed, _ = Scrubber(EXTRA_SCRUB_PATTERNS, keep_trading_sessions).scrub_value(
        ("attributes",), attributes
    )
    return scrubbed


def test_a_trading_session_survives():
    assert scrub("the trading session closed") == "the trading session closed"
    assert scrub("Session 2026-02-02 open") == "Session 2026-02-02 open"


@pytest.mark.parametrize(
    "value",
    [
        # Logfire passes only the FIRST match: a later secret must not ride on "session".
        f"session note: api_key={SENTINEL}",
        "session opened; check the authorization header",
        f"trading session ends, password {SENTINEL}",
        "authorization",
        f"PYDANTIC_AI_GATEWAY_API_KEY={SENTINEL}",
        f"secret {SENTINEL}",
        f"LOGFIRE_TOKEN {SENTINEL}",
        f"BAZAAR_RUNNER_TOKEN={SENTINEL}",
        f"x-bazaar-admin-token: {SENTINEL}",
        f"session closed; runner_token {SENTINEL}",
        "session closed; x-bazaar-approval 6f1c0e2a",
    ],
)
def test_every_other_pattern_is_still_scrubbed(value):
    assert SENTINEL not in scrub(value) and scrub(value).startswith("[Scrubbed")


def test_a_secret_in_the_key_path_is_not_kept_for_a_session_value():
    assert scrub("session open", key="api_key").startswith("[Scrubbed")


def test_bare_secret_shaped_values_are_never_scrubbed():
    # Logfire's patterns match names (api_key, auth, secret), not bare values: redact() is what
    # removes the values themselves from exception text.
    assert scrub("sk-ant-api03-not-a-real-key") == "sk-ant-api03-not-a-real-key"
    assert scrub("9a8b7c6d5e4f") == "9a8b7c6d5e4f"


def test_scrubbing_options_use_the_callback_and_the_extra_patterns():
    options = scrubbing_options()
    assert options.callback is keep_trading_sessions
    assert options.extra_patterns == list(EXTRA_SCRUB_PATTERNS)
    assert all(p not in ("token", "approval") for p in EXTRA_SCRUB_PATTERNS)


@pytest.mark.parametrize(
    "key",
    [
        "http.request.header.x-bazaar-runner-token",
        "http.request.header.x-bazaar-admin-token",
        "http.request.header.x-bazaar-approval",
        "http.request.header.authorization",
    ],
)
def test_secret_header_values_are_scrubbed_by_their_name(key):
    # A neutral value: the header name alone must cause the scrub.
    (value,) = scrub_attributes({key: ("9a8b7c6d5e4f",)})[key]
    assert value.startswith("[Scrubbed")


def test_the_approval_id_is_kept_outside_the_header():
    attributes = {"bazaar.approval_id": "6f1c0e2a", "approval_id": "6f1c0e2a"}
    assert scrub_attributes(attributes) == attributes


def test_token_usage_and_strategy_attributes_are_not_secrets():
    attributes = {
        "gen_ai.usage.input_tokens": 123,
        "gen_ai.aggregated_usage.output_tokens": 45,
        "total_tokens": 168,
        "bazaar.experiment_id": "0c5f",
        "bazaar.strategy_name": "momo",
    }
    assert scrub_attributes(attributes) == attributes


# Redaction of secret values.


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(telemetry, "_secrets", (GATEWAY_KEY, SENTINEL))


def test_redact_removes_every_configured_value_and_extra_secret(secrets):
    text = f"failed: {GATEWAY_KEY} and {SENTINEL} and extra-secret-1234"
    assert redact(text, "extra-secret-1234") == f"failed: {REDACTED} and {REDACTED} and {REDACTED}"


def test_redact_removes_values_named_like_secrets(secrets):
    assert redact("api_key=abc123 x") == f"api_key={REDACTED} x"
    assert redact("Authorization: Bearer abc123") == f"Authorization: {REDACTED}"
    assert redact("runner_token: abc123") == f"runner_token: {REDACTED}"
    # Quoted values (reviewer N1): the opening quote stays, the value goes.
    assert redact('{"api_key": "abc123secretvalue"}') == f'{{"api_key": "{REDACTED}"}}'
    assert redact('runner_token="zzzzzzzzzzzz"') == f'runner_token="{REDACTED}"'
    assert redact("password='hunter22' next") == f"password='{REDACTED}' next"


def test_redact_keeps_ordinary_text(secrets):
    for text in (
        "the trading session closed",
        "unauthorized: runner token required",
        "ConnectError: connection refused",
        "while opening the run, the market said: not approved",
    ):
        assert redact(text) == text
    # Short values are not credentials: an empty or tiny extra secret never redacts text.
    assert redact("a b c", "", "a", None) == "a b c"


def test_secrets_are_read_from_the_environment_once(monkeypatch):
    monkeypatch.setattr(telemetry, "_secrets", None)
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", GATEWAY_KEY)
    monkeypatch.setenv("BAZAAR_ADMIN_TOKEN", "short")
    assert redact(f"x {GATEWAY_KEY}") == f"x {REDACTED}"
    assert telemetry._secrets is not None and GATEWAY_KEY in telemetry._secrets
    assert "short" not in telemetry._secrets
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "changed-later-value")
    assert redact("changed-later-value") == "changed-later-value"


def test_managed_variable_api_key_is_redacted(monkeypatch):
    monkeypatch.setattr(telemetry, "_secrets", None)
    monkeypatch.setenv("LOGFIRE_API_KEY", VARIABLES_KEY)
    assert redact(f"request failed with {VARIABLES_KEY}") == f"request failed with {REDACTED}"


def test_a_context_hidden_by_from_none_is_not_recorded_so_the_type_is_kept(secrets):
    # A traceback never prints a suppressed context, so a cleaned error (like the market port's
    # redacted MarketError) keeps its type and message.
    with pytest.raises(RuntimeError) as caught, redacted_exceptions():
        try:
            raise ValueError(f"inner {SENTINEL}")
        except ValueError:
            raise RuntimeError("outer, no secret here") from None
    assert str(caught.value) == "outer, no secret here"


def test_an_unsuppressed_context_with_a_secret_is_redacted(secrets):
    with pytest.raises(RedactedError) as caught, redacted_exceptions():
        try:
            raise ValueError(f"inner {SENTINEL}")
        except ValueError:
            raise RuntimeError("outer, no secret here")
    assert str(caught.value) == "RuntimeError: outer, no secret here"


def test_a_secret_anywhere_in_the_chain_is_redacted(secrets):
    with pytest.raises(RedactedError) as caught, redacted_exceptions():
        try:
            raise ValueError(f"inner {GATEWAY_KEY}")
        except ValueError as exc:
            raise RuntimeError("outer, no secret here") from exc
    assert str(caught.value) == "RuntimeError: outer, no secret here"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_secret_in_the_message_is_redacted(secrets):
    with pytest.raises(RedactedError) as caught, redacted_exceptions("extra-secret-1234"):
        raise ConnectionError(f"refused {GATEWAY_KEY} extra-secret-1234")
    assert str(caught.value) == f"ConnectionError: refused {REDACTED} {REDACTED}"


def test_an_exception_without_secrets_keeps_its_type(secrets):
    with pytest.raises(KeyError), redacted_exceptions():
        raise KeyError("missing symbol")


# Configure once per process.


@pytest.fixture
def unconfigured(monkeypatch):
    """A process nobody has configured. Nothing is sent: no token, no credentials directory."""
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    monkeypatch.setattr(GLOBAL_CONFIG, "_initialized", False)


def test_configure_once_the_first_service_name_wins(unconfigured):
    assert telemetry.configure("bazaar-first") is True
    assert telemetry.configure("bazaar-second") is False
    assert GLOBAL_CONFIG.service_name == "bazaar-first"


def test_configure_never_replaces_a_direct_logfire_configuration(unconfigured):
    logfire.configure(send_to_logfire=False, console=False, service_name="bazaar-web")
    assert telemetry.configure("bazaar-runner") is False
    assert GLOBAL_CONFIG.service_name == "bazaar-web"


def test_configure_sets_the_shared_scrubbing(unconfigured):
    assert telemetry.configure("bazaar-protocol") is True
    assert GLOBAL_CONFIG.scrubbing.callback is keep_trading_sessions
    assert GLOBAL_CONFIG.scrubbing.extra_patterns == list(EXTRA_SCRUB_PATTERNS)
    assert GLOBAL_CONFIG.service_version == "0.1.0"


def test_managed_variables_use_a_short_bounded_timeout(unconfigured, monkeypatch):
    monkeypatch.setenv("LOGFIRE_API_KEY", VARIABLES_KEY)
    seen = {}
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: seen.update(kwargs))

    assert telemetry.configure("bazaar-runner", managed_variables=True) is True

    assert seen["variables"].timeout == (2, 2)


def test_managed_variables_are_not_started_without_an_api_key(unconfigured, monkeypatch):
    monkeypatch.delenv("LOGFIRE_API_KEY", raising=False)
    seen = {}
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: seen.update(kwargs))

    assert telemetry.configure("bazaar-runner", managed_variables=True) is True

    assert "variables" not in seen


def test_the_redacted_error_references_no_original_exception(secrets):
    # Reviewer N3: not even a suppressed __context__ points at the secret-bearing error.
    with pytest.raises(RedactedError) as caught, redacted_exceptions():
        raise ConnectionError(f"refused {GATEWAY_KEY}")
    error = caught.value
    assert error.__cause__ is None and error.__context__ is None and error.__suppress_context__
    assert GATEWAY_KEY not in str(error)


def test_a_clean_block_leaves_nothing_behind(secrets):
    with redacted_exceptions():
        value = 1
    assert value == 1
