import logging
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from bazaar_market.app import create_app
from bazaar_market.dev_grants import DevAllowListGrants
from fastapi.testclient import TestClient


def test_off_unless_the_variable_is_set():
    assert DevAllowListGrants.from_env({}) is None
    assert DevAllowListGrants.from_env({"BAZAAR_DEV_APPROVAL_IDS": " , "}) is None


def test_a_malformed_id_fails_at_startup():
    with pytest.raises(ValueError):
        DevAllowListGrants.from_env({"BAZAAR_DEV_APPROVAL_IDS": "not-a-uuid"})


def test_allows_only_listed_ids_and_warns_each_time(caplog):
    listed = uuid4()
    grants = DevAllowListGrants.from_env({"BAZAAR_DEV_APPROVAL_IDS": f"{uuid4()}, {listed}"})
    with caplog.at_level(logging.WARNING, logger="bazaar_market.dev_grants"):
        assert grants.allows(listed)
        assert grants.allows(listed)
        assert not grants.allows(uuid4())
    assert caplog.text.count(f"DEV allow-list approved approval_id={listed}") == 2


def test_the_app_uses_the_allow_list_only_when_set(tmp_path, monkeypatch):
    listed = uuid4()
    body = {"cutoff": "2025-07-01T20:00:00Z", "data_version": "none", "execution_rule_version": "x"}
    headers = {"X-Bazaar-Approval": str(listed)}

    monkeypatch.delenv("BAZAAR_DEV_APPROVAL_IDS", raising=False)
    with TestClient(create_app(tmp_path / "off.db")) as client:
        assert (
            client.put(f"/experiments/{uuid4()}/cutoff", json=body, headers=headers).status_code
            == 403
        )

    monkeypatch.setenv("BAZAAR_DEV_APPROVAL_IDS", str(listed))
    with TestClient(create_app(tmp_path / "on.db")) as client:
        # Past the approval check; refused only because the data version has no bars.
        assert (
            client.put(f"/experiments/{uuid4()}/cutoff", json=body, headers=headers).status_code
            == 422
        )
    with closing(sqlite3.connect(tmp_path / "on.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM acct_experiments").fetchone() == (0,)
