"""Keep the workspace test suite independent of paid judge calls."""

import pytest


@pytest.fixture(autouse=True)
def disable_strategy_judge(monkeypatch):
    monkeypatch.setenv("BAZAAR_STRATEGY_EVAL_ENABLED", "0")
