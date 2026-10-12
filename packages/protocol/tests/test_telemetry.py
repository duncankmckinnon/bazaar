"""Shared Logfire setup: configure once, scrub only real secrets (SC1), redact values."""

import json

import httpx
import logfire
import pytest
from bazaar_protocol import telemetry
from bazaar_protocol.telemetry import (
    EMAIL_SCRUBBED,
    REDACTED,
    RedactedError,
    precise_scrub,
    redact,
    redacted_exceptions,
    scrubbing_options,
)
from logfire._internal.config import GLOBAL_CONFIG
from logfire._internal.scrubbing import Scrubber

SENTINEL = "sentinel-runner-token-9a8b"
GATEWAY_KEY = "sentinel-gateway-key-0f1e"
VARIABLES_KEY = "sentinel-variables-key-6d2c"
LONG_SECRET = "sk-sentinel-0123456789abcdefXYZ"  # 30 chars: a pattern


def scrubber() -> Scrubber:
    """Logfire's scrubber with the shared options (and the secrets loaded right now)."""
    options = scrubbing_options()
    return Scrubber(options.extra_patterns, options.callback)


def scrub(value, key: str = "message"):
    scrubbed, _ = scrubber().scrub_value(("attributes", key), value)
    return scrubbed


def scrub_attributes(attributes: dict) -> dict:
    """As Logfire scrubs a span: attribute names are checked only inside the mapping."""
    scrubbed, _ = scrubber().scrub_value(("attributes",), attributes)
    return scrubbed


@pytest.fixture(autouse=True)
def no_loaded_secrets(monkeypatch):
    """Each test starts with no secret values loaded unless it loads its own."""
    monkeypatch.setattr(telemetry, "_secrets", ())


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
def test_a_loaded_secret_is_scrubbed_wherever_it_rides(value, monkeypatch):
    # SC1: these used to be scrubbed for a word ("session", "secret", "auth"); now the secret
    # VALUE is what is scrubbed, whatever words surround it.
    monkeypatch.setattr(telemetry, "_secrets", (SENTINEL,))
    if SENTINEL in value:
        assert scrub(value) == REDACTED
    else:
        assert scrub(value) == value  # free text is never scrubbed for a word


@pytest.mark.parametrize(
    "value",
    [
        "zz-sentinel@example.com",
        "contact Alice.Smith+bazaar@Mail.Example.CO.uk for the prize",
        "trading session closed; winner alice@example.com",  # not kept for "session"
    ],
)
def test_an_email_value_is_scrubbed_without_copying_it(value):
    scrubbed = scrub(value)
    assert scrubbed.startswith("[Scrubbed")
    assert "@" not in scrubbed and "example" not in scrubbed.lower()  # no "due to '<email>'"
    (in_span,) = scrub_attributes({"note": (value,)})["note"]
    assert "@" not in in_span


def test_an_email_scrub_leaves_no_note_with_the_address():
    scrubbed, notes = scrubber().scrub_value(("attributes",), {"email": "zz-sentinel@example.com"})
    assert scrubbed == {"email": EMAIL_SCRUBBED}
    assert "zz-sentinel" not in repr(notes) and "example.com" not in repr(notes)


def test_the_email_pattern_leaves_names_and_ordinary_text_alone():
    attributes = {
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 80,
        "strategy": "buy KO @ $60 on dips",
        "handle": "@alice",
    }
    assert scrub_attributes(attributes) == attributes
    assert scrub("the trading session closed") == "the trading session closed"


def test_a_credential_keys_value_is_scrubbed_whatever_it_says():
    assert scrub("session open", key="api_key") == REDACTED


def test_bare_secret_shaped_values_that_are_not_loaded_are_kept():
    # Only LOADED secret values are patterns; a look-alike is ordinary text.
    assert scrub("sk-ant-api03-not-a-real-key") == "sk-ant-api03-not-a-real-key"
    assert scrub("9a8b7c6d5e4f") == "9a8b7c6d5e4f"


def test_scrubbing_options_use_the_callback_and_the_extra_patterns(monkeypatch):
    monkeypatch.setattr(telemetry, "_secrets", (LONG_SECRET,))
    options = scrubbing_options()
    assert options.callback is precise_scrub
    assert options.extra_patterns[0] == telemetry.EMAIL_PATTERN
    # No word patterns: only the key triggers and the loaded secret.
    assert not {r"runner[._ -]?token", r"admin[._ -]?token", r"read[._ -]?token"} & set(
        options.extra_patterns
    )
    assert len(options.extra_patterns) == 1 + len(telemetry._KEY_TRIGGERS) + 1


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
    assert scrub_attributes({key: ("9a8b7c6d5e4f",)})[key] == (REDACTED,)


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
    assert GLOBAL_CONFIG.scrubbing.callback is precise_scrub
    assert GLOBAL_CONFIG.scrubbing.extra_patterns[0] == telemetry.EMAIL_PATTERN
    assert GLOBAL_CONFIG.service_version == "0.1.0"


def test_an_exported_span_carries_no_email(unconfigured):
    """Through a real Logfire configuration with the shared scrubbing, as a process exports."""
    from logfire.testing import TestExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=scrubbing_options(),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    try:
        logfire.info("contact {note}", note="write to zz-sentinel@example.com", email="z@x.io")
        with logfire.span("probe", winner="ZZ.Sentinel+1@Example.co.uk", usage_tokens=12):
            pass
    finally:
        logfire.configure(send_to_logfire=False, console=False)

    dumped = json.dumps(exporter.exported_spans_as_dict(), default=str).lower()
    assert "zz-sentinel" not in dumped and "zz.sentinel" not in dumped and "z@x.io" not in dumped
    assert "example.co" not in dumped
    assert "[scrubbed email]" in dumped
    assert '"usage_tokens": 12' in dumped


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


# Market clients: traced per client, transport errors redacted inside the HTTP span.


def failing_client(message: str) -> httpx.AsyncClient:
    def fail(request):
        raise httpx.ConnectError(message, request=request)

    return telemetry.instrument_market_client(
        httpx.AsyncClient(base_url="http://market", transport=httpx.MockTransport(fail))
    )


async def test_a_transport_error_holding_a_secret_keeps_its_type_redacted(secrets, capfire):
    async with failing_client(f"refused {GATEWAY_KEY}") as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await client.get("/health")
    assert str(caught.value) == f"refused {REDACTED}"
    assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_a_transport_error_without_secrets_is_untouched(secrets, capfire):
    async with failing_client("connection refused") as client:
        with pytest.raises(httpx.ConnectError, match="^connection refused$"):
            await client.get("/health")


async def test_instrumenting_clients_never_rewraps_a_shared_transport(capfire):
    shared = httpx.MockTransport(lambda request: httpx.Response(204))
    clients = [httpx.AsyncClient(transport=shared) for _ in range(3)]
    for client in clients:
        telemetry.instrument_market_client(client)
    # Each client wraps the shared transport in its own redacting transport; the shared one
    # itself is never patched.
    assert "handle_async_request" not in vars(shared)
    assert len({id(client._transport) for client in clients}) == 3


# The strategist's Logfire read token.

READ_TOKEN = "pylf_v1_us_sentinelreadtoken0123456789"


@pytest.mark.parametrize("name", ["logfire_read_token", "LOGFIRE_READ_TOKEN", "read-token"])
def test_an_attribute_named_like_the_read_token_is_scrubbed(name):
    scrubbed = scrub_attributes({name: READ_TOKEN, "gen_ai.usage.input_tokens": 12})
    assert READ_TOKEN not in str(scrubbed) and scrubbed["gen_ai.usage.input_tokens"] == 12


def test_the_read_token_value_is_redacted_from_an_exception(monkeypatch):
    monkeypatch.setattr(telemetry, "_secrets", None)
    monkeypatch.setenv("LOGFIRE_READ_TOKEN", READ_TOKEN)
    with pytest.raises(telemetry.RedactedError) as caught, redacted_exceptions():
        raise RuntimeError(f"401 from /v2/query for {READ_TOKEN}")
    assert READ_TOKEN not in str(caught.value) and REDACTED in str(caught.value)
    assert caught.value.__context__ is None


# The strategist eval's Logfire API write key (dataset push).

WRITE_KEY = "pylf_v1_us_sentinelwritekey0123456789"


@pytest.mark.parametrize("name", ["logfire_api_write_key", "LOGFIRE_API_WRITE_KEY", "api-key"])
def test_an_attribute_named_like_the_write_key_is_scrubbed(name, monkeypatch):
    # SC1: the loaded VALUE is scrubbed (a key named "...api_write_key" is not a credential key).
    monkeypatch.setattr(telemetry, "_secrets", (WRITE_KEY,))
    scrubbed = scrub_attributes({name: WRITE_KEY, "gen_ai.usage.input_tokens": 12})
    assert WRITE_KEY not in str(scrubbed) and scrubbed["gen_ai.usage.input_tokens"] == 12


def test_the_write_key_value_is_redacted_from_an_exception(monkeypatch):
    assert "LOGFIRE_API_WRITE_KEY" in telemetry.SECRET_ENV
    monkeypatch.setattr(telemetry, "_secrets", None)
    monkeypatch.setenv("LOGFIRE_API_WRITE_KEY", WRITE_KEY)
    with pytest.raises(telemetry.RedactedError) as caught, redacted_exceptions():
        raise RuntimeError(f"401 from /v1/datasets for {WRITE_KEY}")
    assert WRITE_KEY not in str(caught.value) and REDACTED in str(caught.value)
    assert caught.value.__context__ is None


# SC1: precise scrubbing, through a real Logfire configuration as a process exports.

SC1_SECRET = "sc1-sentinel-secret-0123456789abcdef"  # 36 chars, loaded from the environment


@pytest.fixture
def exported(monkeypatch):
    """Configure Logfire with the shared scrubbing (secrets loaded from the environment then) and
    return a function that reads every exported span as one JSON text plus the spans."""
    from logfire.testing import TestExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setenv("BAZAAR_RUNNER_TOKEN", SC1_SECRET)
    monkeypatch.setattr(telemetry, "_secrets", None)
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=scrubbing_options(),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    yield exporter
    logfire.configure(send_to_logfire=False, console=False)


def by_name(exporter, name):
    (span,) = [s for s in exporter.exported_spans_as_dict() if s["name"] == name]
    return span["attributes"]


def test_a_loaded_secret_is_scrubbed_everywhere_in_a_span(exported):
    with logfire.span(
        "calling with {note}",
        _span_name="secret probe",
        note=f"the token is {SC1_SECRET} today",  # free text, and in the message
        exact=SC1_SECRET,  # the whole value
        nested={"request": {"headers": [f"Bearer {SC1_SECRET}"]}},  # nested
    ):
        pass
    dumped = json.dumps(exported.exported_spans_as_dict(), default=str)
    assert SC1_SECRET not in dumped and SC1_SECRET[:-1] not in dumped  # notes included
    attributes = by_name(exported, "secret probe")
    assert attributes["exact"] == REDACTED and attributes["note"] == REDACTED
    assert REDACTED in attributes["logfire.msg"]
    assert REDACTED in attributes["nested"]


def test_credential_headers_are_scrubbed_by_name(exported):
    with logfire.span(
        "headers",
        **{
            "http.request.header.authorization": ("Bearer not-a-loaded-value",),
            "http.request.header.x-bazaar-runner-token": ("not-loaded-either",),
            "x-bazaar-admin-token": "plain",
            "cookie": "a=b",
        },
    ):
        pass
    attributes = by_name(exported, "headers")
    # logfire.span stores a tuple as a JSON string (httpx sets a real tuple): REDACTED either way.
    assert attributes["http.request.header.authorization"] == REDACTED
    assert attributes["http.request.header.x-bazaar-runner-token"] == REDACTED
    assert attributes["x-bazaar-admin-token"] == REDACTED and attributes["cookie"] == REDACTED
    dumped = json.dumps(exported.exported_spans_as_dict(), default=str)
    assert "not-a-loaded-value" not in dumped and "not-loaded-either" not in dumped


def test_token_counts_survive(exported):
    usage = {
        "gen_ai.usage.details.cache_read_tokens": 512,
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 80,
        "gen_ai.aggregated_usage.cache_write_tokens": 7,
        "total_tokens": 1792,
    }
    with logfire.span("chat probe", **usage):
        pass
    attributes = by_name(exported, "chat probe")
    assert {k: attributes[k] for k in usage} == usage
    assert "logfire.scrubbed" not in attributes


CONTENT = (
    "Maximize market-authoritative portfolio value. Social Security trust data; the secret sauce "
    "is patience. The trading session closed early; a cookie jar was on the desk. Credentials "
    "verified by the auditor; private key insight: password resets are frequent."
)


def test_free_text_is_never_scrubbed_for_a_word(exported):
    with logfire.span("judge {text}", _span_name="judge probe", text=CONTENT, role=CONTENT):
        pass
    attributes = by_name(exported, "judge probe")
    assert attributes["text"] == CONTENT and attributes["role"] == CONTENT
    # The message truncates long values; what it keeps is unscrubbed.
    assert attributes["logfire.msg"].startswith("judge Maximize market-authoritative portfolio")
    assert "Scrubbed" not in attributes["logfire.msg"] and REDACTED not in attributes["logfire.msg"]
    assert "logfire.scrubbed" not in attributes


def test_an_email_is_still_scrubbed_and_appears_nowhere(exported):
    with logfire.span("contact {who}", _span_name="email probe", who="mail zz-sc1@example.org now"):
        pass
    attributes = by_name(exported, "email probe")
    assert attributes["who"] == EMAIL_SCRUBBED
    dumped = json.dumps(exported.exported_spans_as_dict(), default=str)
    assert "zz-sc1" not in dumped and "example.org" not in dumped


def test_a_short_secret_is_not_a_pattern(monkeypatch):
    short = "sk-short-12345"  # 14 chars: below MIN_SCRUB_SECRET_LENGTH
    monkeypatch.setattr(telemetry, "_secrets", (short, SC1_SECRET))
    patterns = scrubbing_options().extra_patterns
    assert not any("short" in p for p in patterns)
    assert any("sc1" in p for p in patterns)
    # Nothing triggers on the short value alone, so plain text holding it is kept as written.
    assert scrub(f"ref {short} ok") == f"ref {short} ok"


@pytest.mark.parametrize(
    ("key", "credential"),
    [
        ("gen_ai.usage.details.cache_read_tokens", False),
        ("gen_ai.usage.input_tokens", False),
        ("output_tokens", False),
        ("cache_write_tokens", False),
        ("total_tokens", False),
        ("gen_ai.aggregated_usage.reasoning_tokens", False),
        ("http.request.header.authorization", True),
        ("Proxy-Authorization", True),
        ("set-cookie", True),
        ("x-api-key", True),
        ("x-bazaar-runner-token", True),
        ("x-bazaar-approval", True),
        ("apikey", True),
        ("password", True),
        ("secret", True),
        ("secret_sauce", True),  # a KEY naming a secret (key-only rule, SC1b)
        ("client_secret", True),
        ("private_key", True),
        ("private-key", True),
        ("credential", True),
        ("aws_credentials", True),
        ("jwt", True),
        ("token", True),
        ("logfire.token", True),
        ("apca-api-secret-key", True),
        ("authorization-foreign", True),
        ("db_passwd", True),
        ("gen_ai.request.max_tokens", False),
        ("gen_ai.usage.details.reasoning_tokens", False),
        ("token_count", False),
        ("logfire_read_token", True),
        ("refresh-token", True),
    ],
)
def test_credential_keys_and_counts(key, credential):
    assert telemetry._credential_key(key) is credential
    # A count reaching the callback (e.g. under a key Logfire matched) is kept as the number.
    if not credential:
        assert precise_scrub(logfire.ScrubMatch(("attributes", key), 512, None)) == 512


SC1B_KEYS = [
    "client_secret",
    "private_key",
    "credential",
    "jwt",
    "token",
    "logfire.token",
    "apca-api-secret-key",
    "authorization-foreign",
]


def test_credential_named_keys_are_redacted_in_an_exported_span(exported):
    """SC1b: a key NAMING a secret has its value redacted, whatever the value says."""
    values = {key: f"plain-value-{i}" for i, key in enumerate(SC1B_KEYS)}
    usage = {
        "gen_ai.usage.details.cache_read_tokens": 512,
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 80,
        "input_tokens": 9,
    }
    prose = (
        "The secret sauce is patience; a private key insight from the desk; the jwt rollout "
        "and a token gesture."
    )
    with logfire.span("keys probe", note=prose, **values, **usage):
        pass
    attributes = by_name(exported, "keys probe")
    assert {k: attributes[k] for k in SC1B_KEYS} == dict.fromkeys(SC1B_KEYS, REDACTED)
    assert {k: attributes[k] for k in usage} == usage
    assert attributes["note"] == prose
    dumped = json.dumps(exported.exported_spans_as_dict(), default=str)
    assert "plain-value-" not in dumped


def test_more_secret_env_names_are_loaded():
    assert {"PAIG_API_KEY", "BAZAAR_CURSOR_SECRET"} <= set(telemetry.SECRET_ENV)
