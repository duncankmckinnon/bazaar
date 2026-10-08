"""Logfire keeps 'trading session' text; every other default pattern stays scrubbed."""

import pytest
from bazaar_market.app import keep_trading_sessions
from logfire._internal.scrubbing import Scrubber

SENTINEL = "sentinel-runner-token-9a8b"


def scrub(value: str, key: str = "message") -> str:
    scrubbed, _ = Scrubber(None, keep_trading_sessions).scrub_value(("attributes", key), value)
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
    ],
)
def test_every_other_pattern_is_still_scrubbed(value):
    assert SENTINEL not in scrub(value) and scrub(value).startswith("[Scrubbed")


def test_a_secret_in_the_key_path_is_not_kept_for_a_session_value():
    assert scrub("session open", key="api_key").startswith("[Scrubbed")


def test_bare_secret_shaped_values_are_never_scrubbed_by_default():
    # Logfire's default patterns match names (api_key, auth, secret), not bare values. These pass
    # with or without our callback; secrets are kept out of spans by never logging them.
    assert scrub("sk-ant-api03-not-a-real-key") == "sk-ant-api03-not-a-real-key"
    assert scrub(SENTINEL) == SENTINEL
