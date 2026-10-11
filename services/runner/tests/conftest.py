import os

import pytest

# The library opens spans but never configures Logfire; only capfire tests configure it.
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")


@pytest.fixture(autouse=True)
def no_operator_bundle(monkeypatch):
    """Hermetic: an operator's $BAZAAR_DATA_BUNDLE in the shell never picks a test's bundle."""
    monkeypatch.delenv("BAZAAR_DATA_BUNDLE", raising=False)
