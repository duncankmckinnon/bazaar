"""v11 S1: server-side name normalisation and suffixing, optional company and role, POST /api/leads."""

import dataclasses
import re
import sqlite3
import threading

import pytest
from bazaar_web import telemetry
from bazaar_web.app import create_app, normalize_name
from bazaar_web.store import OPERATOR_CONTACT_EMAIL, Contact, Store
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}
SUFFIXED = re.compile(r"-[a-z0-9]{4}$")


def body(name="alice-bot", **changes):
    base = {"name": name, "instructions": "Buy KO on dips, hold MSFT.", "email": "a@example.com"}
    return base | changes


def submit(client, ip="10.0.0.1", **changes):
    return client.post("/api/submissions", json=body(**changes), headers={"X-Forwarded-For": ip})


def lead(client, email, ip="10.0.0.1", **extra):
    return client.post(
        "/api/leads", json={"email": email, **extra}, headers={"X-Forwarded-For": ip}
    )


def fresh(settings, sql, params=()):
    """Read with a new connection, outside the app's store."""
    with sqlite3.connect(settings.web_db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params)]


# ---------- the name ----------


@pytest.mark.parametrize(
    ("given", "normalised"),
    [
        ("Tech Momentum!!", "tech-momentum"),
        ("  --a  b--  ", "a-b"),
        ("alice-bot", "alice-bot"),
        ("Buy\tthe\n\nDIP", "buy-the-dip"),
        ("a - - b", "a-b"),
        ("Ünïcode Ok", "ncode-ok"),  # dropped, as the prototype drops it
        ("x" * 60, "x" * 40),
        ("a" * 39 + " b", "a" * 39),  # cut to 40 ends on "-", which is stripped
    ],
)
def test_names_are_normalised_like_the_form(settings, helpers, given, normalised):
    assert normalize_name(given) == normalised
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = submit(client, name=given)

    assert response.status_code == 201, response.text
    assert (response.json()["name"], response.json()["renamed"]) == (normalised, False)
    assert Store(settings.web_db).get(response.json()["id"])["name"] == normalised


@pytest.mark.parametrize("given", ["AB", "!!", "  -- ", "a!b", "", "x" * 201])
def test_a_name_that_normalises_short_or_is_too_long_is_a_422(settings, helpers, given):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = submit(client, name=given)

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["loc"], error["msg"]) == (
        ["body", "name"],
        "Use 3 to 40 letters, digits or hyphens",
    )


def test_200_characters_are_accepted(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = submit(client, name="y" * 200)

    assert response.status_code == 201
    assert response.json()["name"] == "y" * 40


@pytest.mark.parametrize("base", ["alice-bot", "z" * 40, "q" * 34 + "-rrrrr"])
def test_a_taken_name_gets_a_random_suffix(settings, helpers, base):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        first = submit(client, name=base)
        second = submit(client, name=base.upper(), ip="10.0.0.2")  # normalises to the same

    assert first.json()["name"] == base
    name = second.json()["name"]
    assert second.status_code == 201 and second.json()["renamed"] is True
    assert SUFFIXED.search(name) and len(name) <= 40 and "--" not in name
    assert name.startswith(base[: len(name) - 5].rstrip("-"))
    assert Store(settings.web_db).get(second.json()["id"])["name"] == name


def test_a_hidden_name_is_still_taken(settings, helpers):
    settings = dataclasses.replace(settings, admin_token="admin-secret")
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        first = submit(client).json()["id"]
        assert client.post(f"/api/admin/submissions/{first}/hide", headers=ADMIN).status_code == 204
        again = submit(client, ip="10.0.0.2").json()

    assert again["renamed"] and SUFFIXED.search(again["name"])


def test_concurrent_duplicates_all_succeed_with_distinct_names(settings, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()  # keep them queued
    start = threading.Barrier(3)
    responses = []

    def post(i):
        start.wait()
        responses.append(submit(client, name="Same Name", ip=f"10.0.1.{i}"))

    with TestClient(create_app(settings, runner)) as client:
        threads = [threading.Thread(target=post, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        runner.gate.set()

    assert [r.status_code for r in responses] == [201] * 3
    names = sorted(r.json()["name"] for r in responses)
    assert len(set(names)) == 3 and names[0] == "same-name"
    assert all(SUFFIXED.search(n) for n in names[1:])
    stored = fresh(settings, "SELECT name FROM submissions ORDER BY name")
    assert [row["name"] for row in stored] == names


def test_the_store_suffixes_after_every_attempt_is_taken(settings, monkeypatch):
    """20 attempts, then NameTaken (the app's 422); a fixed suffix makes every retry collide."""
    import bazaar_web.store as store_module

    store = Store(settings.web_db)
    common = {"handle": None, "instructions": "x" * 20, "max_queue": 99, "max_per_day": 99}
    store.create(name="taken", ip_hash="a", max_per_ip_hour=99, **common)
    monkeypatch.setattr(store_module, "name_suffix", lambda: "aaaa")
    store.create(name="taken", ip_hash="b", max_per_ip_hour=99, **common)  # taken-aaaa
    with pytest.raises(store_module.NameTaken):
        store.create(name="taken", ip_hash="c", max_per_ip_hour=99, **common)


# ---------- company and role ----------


def test_old_clients_sending_company_and_role_still_work(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = submit(client, company=" Example Co ", role="Engineer", handle="@a")

    assert response.status_code == 201
    assert set(response.json()) == {"id", "status", "position", "name", "renamed"}
    row = Store(settings.web_db).get(response.json()["id"])
    assert (row["company"], row["role"]) == ("Example Co", "Engineer")


# ---------- POST /api/leads ----------


def test_a_lead_is_stored_normalised_and_the_reply_is_an_uncached_204(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = lead(client, "  Ada@Example.COM ")

    assert (response.status_code, response.content) == (204, b"")
    assert response.headers["cache-control"] == "no-store"
    [row] = fresh(settings, "SELECT * FROM leads")
    assert row["email"] == "ada@example.com"
    assert row["created_at"] == row["last_seen_at"]
    assert len(row["ip_hash"]) == 64 and "10.0.0.1" not in row["ip_hash"]


def test_reposting_a_lead_only_moves_last_seen_at(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert lead(client, "ada@example.com").status_code == 204
        [before] = fresh(settings, "SELECT * FROM leads")
        assert lead(client, "ADA@example.com", ip="10.0.0.2").status_code == 204

    [after] = fresh(settings, "SELECT * FROM leads")
    assert {k: after[k] for k in ("id", "email", "created_at", "ip_hash")} == {
        k: before[k] for k in ("id", "email", "created_at", "ip_hash")
    }
    assert after["last_seen_at"] >= before["last_seen_at"]


@pytest.mark.parametrize(
    "payload",
    [{"email": "nope"}, {"email": ""}, {}, {"email": "a@b.co", "extra": 1}, {"email": 5}],
)
def test_an_invalid_lead_is_a_422_without_an_input_echo(settings, helpers, payload):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.post("/api/leads", json=payload)

    assert response.status_code == 422
    assert all(set(e) == {"type", "loc", "msg"} for e in response.json()["detail"])
    assert fresh(settings, "SELECT * FROM leads") == []


def test_leads_have_their_own_hourly_cap_per_network(settings, helpers):
    settings = dataclasses.replace(settings, max_per_ip_hour=1, max_leads_per_ip_hour=3)
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        # The submission fills its own cap; its lead counts toward the lead cap too.
        assert submit(client, email="s@example.com").status_code == 201
        assert submit(client, name="next-bot").status_code == 429
        assert [lead(client, f"p{i}@example.com").status_code for i in range(3)] == [204, 204, 429]
        refused = lead(client, "s@example.com")  # known or not, over the cap is a 429
        other = lead(client, "p9@example.com", ip="10.0.0.2")  # another network has room

    assert refused.status_code == 429
    assert refused.headers["cache-control"] == "no-store"
    assert refused.json() == {
        "detail": "Too many sign-ups from your network in the last hour. Please wait a bit."
    }
    assert other.status_code == 204


def test_the_lead_cap_comes_from_the_environment(monkeypatch):
    from bazaar_web.settings import Settings

    monkeypatch.delenv("BAZAAR_MAX_LEADS_PER_IP_PER_HOUR", raising=False)
    assert Settings.from_env().max_leads_per_ip_hour == 60
    monkeypatch.setenv("BAZAAR_MAX_LEADS_PER_IP_PER_HOUR", "200")
    assert Settings.from_env().max_leads_per_ip_hour == 200


def test_a_submission_links_its_lead_made_before_or_with_it(settings, helpers):
    settings = dataclasses.replace(settings, admin_token="admin-secret")
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert lead(client, "Ada@Example.com").status_code == 204
        early = submit(client, name="ada-bot", email="ada@example.com").json()["id"]
        late = submit(client, name="bob-bot", email="bob@example.com").json()["id"]
        helpers.wait_for(
            lambda: client.get(f"/api/submissions/{early}").json()["status"] == "scored"
        )
        rerun = client.post(f"/api/admin/submissions/{early}/rerun", headers=ADMIN).json()["id"]

    leads = {r["email"]: r["id"] for r in fresh(settings, "SELECT * FROM leads")}
    assert set(leads) == {"ada@example.com", "bob@example.com"}
    links = {r["id"]: r["lead_id"] for r in fresh(settings, "SELECT id, lead_id FROM submissions")}
    assert links[early] == links[rerun] == leads["ada@example.com"]
    assert links[late] == leads["bob@example.com"]


def test_leads_without_a_submission_are_counted(settings):
    store = Store(settings.web_db)
    common = {"handle": None, "instructions": "x" * 20, "max_queue": 99, "max_per_day": 99}
    for email in ("lonely@example.com", "taken@example.com", OPERATOR_CONTACT_EMAIL):
        store.add_lead(email=email, ip_hash="h", max_per_ip_hour=99)
    store.create(
        name="taken-bot",
        ip_hash="h",
        max_per_ip_hour=99,
        contact=Contact(email="taken@example.com"),
        **common,
    )
    store.create(name="anon-bot", ip_hash="h", max_per_ip_hour=99, **common)  # no email, no lead

    assert store.leads_without_submission() == 1
    assert len(fresh(settings, "SELECT * FROM leads")) == 3


def test_an_older_database_gains_the_leads_table_and_lead_id(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, latest_value TEXT, trace_context TEXT, "
            "rerun_of TEXT);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'scored', '2026-10-08T14:00:00+00:00');"
        )
    conn.close()

    store = Store(path)
    assert store.get("old1")["lead_id"] is None
    store.add_lead(email="new@example.com", ip_hash="h", max_per_ip_hour=5)
    assert store.leads_without_submission() == 1


def test_the_leads_route_is_untraced_in_the_backend():
    for url in ("/api/leads", "https://bazaar.example/api/leads", "/api/leads?x=1"):
        assert any(re.search(p, url) for p in telemetry.EXCLUDED_URLS), url
    assert not any(re.search(p, "/api/leadsx") for p in telemetry.EXCLUDED_URLS)
    assert not any(re.search(p, "/api/submissions") for p in telemetry.EXCLUDED_URLS)


def test_a_lead_makes_no_span_at_all(settings, helpers, capfire):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        capfire.exporter.clear()
        assert lead(client, "zz-lead-sentinel@example.com").status_code == 204
        assert lead(client, "not-an-email").status_code == 422

    assert capfire.exporter.exported_spans_as_dict() == []
