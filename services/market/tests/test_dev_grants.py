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


@pytest.mark.parametrize("value", ["not-a-uuid:also-not", str(uuid4()), f"{uuid4()}:nope"])
def test_a_malformed_entry_fails_at_startup(value):
    with pytest.raises(ValueError):
        DevAllowListGrants.from_env({"BAZAAR_DEV_APPROVAL_IDS": value})


def test_allows_only_listed_pairs_and_warns_each_time(caplog):
    approval, experiment = uuid4(), uuid4()
    grants = DevAllowListGrants.from_env(
        {"BAZAAR_DEV_APPROVAL_IDS": f"{uuid4()}:{uuid4()}, {approval}:{experiment}"}
    )
    with caplog.at_level(logging.WARNING, logger="bazaar_market.dev_grants"):
        assert grants.allows(approval, experiment)
        assert grants.allows(approval, experiment)
        assert not grants.allows(approval, uuid4())
        assert not grants.allows(uuid4(), experiment)
    assert caplog.text.count(f"approval_id={approval} experiment_id={experiment}") == 2


def test_the_app_uses_the_allow_list_only_when_set(tmp_path, monkeypatch):
    approval, experiment = uuid4(), uuid4()
    body = {"cutoff": "2025-07-01T20:00:00Z", "data_version": "none", "execution_rule_version": "x"}
    headers = {"X-Bazaar-Approval": str(approval), "X-Bazaar-Runner-Token": "t"}
    url = f"/experiments/{experiment}/cutoff"

    monkeypatch.delenv("BAZAAR_DEV_APPROVAL_IDS", raising=False)
    with TestClient(create_app(tmp_path / "off.db", runner_token="t")) as client:
        assert client.put(url, json=body, headers=headers).status_code == 403

    monkeypatch.setenv("BAZAAR_DEV_APPROVAL_IDS", f"{approval}:{experiment}")
    with TestClient(create_app(tmp_path / "on.db", runner_token="t")) as client:
        # Past the approval check; refused only because the data version has no bars.
        assert client.put(url, json=body, headers=headers).status_code == 422
        other = f"/experiments/{uuid4()}/cutoff"
        assert client.put(other, json=body, headers=headers).status_code == 403
    with closing(sqlite3.connect(tmp_path / "on.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM acct_experiments").fetchone() == (0,)
