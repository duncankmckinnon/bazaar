"""v11 E1: leads in the contacts CSV, and a 2xx only after the row has committed."""

import csv
import io
import logging
import sqlite3
from datetime import timedelta

from bazaar_web.app import create_app
from bazaar_web.store import OPERATOR_CONTACT_EMAIL
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}


def body(name, email):
    return {"name": name, "instructions": "Buy KO on dips, hold MSFT.", "email": email}


def contacts(client):
    response = client.get("/api/admin/contacts", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return list(csv.DictReader(io.StringIO(response.text)))


def committed(settings, sql, params=()):
    """Rows another connection sees, so only committed ones."""
    conn = sqlite3.connect(settings.web_db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------- the contacts CSV ----------


def test_the_csv_has_one_row_per_email_with_its_source_and_earliest_time(settings, helpers):
    clock = helpers.Clock()
    start = clock.at
    with TestClient(create_app(settings, helpers.FakeRunner(), now=clock)) as client:
        assert client.post("/api/leads", json={"email": "Lead.Only@example.com"}).status_code == 204
        clock.at += timedelta(minutes=1)
        assert client.post("/api/leads", json={"email": "later@example.com"}).status_code == 204
        clock.at += timedelta(minutes=1)
        sub = client.post("/api/submissions", json=body("sub-bot", "sub@example.com"))
        clock.at += timedelta(minutes=1)
        late = client.post("/api/submissions", json=body("later-bot", "LATER@example.com"))
        clock.at += timedelta(minutes=1)
        # Posting a lead again after submitting adds no row.
        assert client.post("/api/leads", json={"email": "sub@example.com"}).status_code == 204
        rows = contacts(client)

    assert (sub.status_code, late.status_code) == (201, 201)
    assert list(rows[0]) == [
        "name", "handle", "email", "company", "role", "created_at", "return_pct", "rank", "source",
    ]  # fmt: skip
    got = [(r["email"], r["source"], r["name"], r["created_at"]) for r in rows]
    assert got == [
        ("sub@example.com", "submission", "sub-bot", (start + timedelta(minutes=2)).isoformat()),
        # Left as a lead at +1, submitted at +3: the submission's row, with the lead's time.
        (
            "later@example.com",
            "submission",
            "later-bot",
            (start + timedelta(minutes=1)).isoformat(),
        ),
        ("lead.only@example.com", "lead", "", start.isoformat()),
    ]
    lead = rows[2]
    assert all(lead[k] == "" for k in ("name", "handle", "company", "role", "return_pct", "rank"))


def test_the_csv_leaves_out_the_operator_lead_and_a_moderated_email(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        client.post("/api/leads", json={"email": OPERATOR_CONTACT_EMAIL.upper()})
        client.post("/api/leads", json={"email": "hidden@example.com"})
        sid = client.post("/api/submissions", json=body("hidden-bot", "hidden@example.com"))
        sid = sid.json()["id"]
        helpers.wait_for(lambda: client.get(f"/api/submissions/{sid}").json()["status"] == "scored")
        assert client.post(f"/api/admin/submissions/{sid}/hide", headers=ADMIN).status_code == 204
        rows = contacts(client)

    assert rows == []  # a hidden submission's email does not come back as a lead


def test_a_lead_email_that_starts_a_formula_is_defused(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert client.post("/api/leads", json={"email": "=cmd@example.com"}).status_code == 204
        [row] = contacts(client)

    assert (row["email"], row["source"]) == ("'=cmd@example.com", "lead")


def test_the_csv_with_leads_is_still_untraced(settings, helpers, capfire):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        client.post("/api/leads", json={"email": "zz-csv-lead-sentinel@example.com"})
        capfire.exporter.clear()
        contacts(client)

    assert capfire.exporter.exported_spans_as_dict() == []


# ---------- 2xx only after commit ----------


def test_a_lead_and_a_submission_answer_only_after_their_rows_commit(settings, helpers):
    """The on_saved hook runs after the transaction and before the reply: it sees the row from
    another connection, and so does a read right after the response."""
    seen = []

    def on_saved():
        seen.append(
            (
                committed(settings, "SELECT count(*) FROM leads")[0][0],
                committed(settings, "SELECT count(*) FROM submissions")[0][0],
            )
        )

    app = create_app(settings, helpers.FakeRunner(), on_saved=on_saved)
    with TestClient(app) as client:
        lead = client.post("/api/leads", json={"email": "ada@example.com"})
        assert committed(settings, "SELECT email FROM leads") == [("ada@example.com",)]
        sub = client.post("/api/submissions", json=body("ada-bot", "bob@example.com"))
        assert committed(settings, "SELECT id FROM submissions") == [(sub.json()["id"],)]

    assert (lead.status_code, sub.status_code) == (204, 201)
    assert seen == [(1, 0), (2, 1)]  # one hook call per new row, each after its commit


def test_refused_requests_ask_for_no_commit(settings, helpers):
    calls = []
    app = create_app(settings, helpers.FakeRunner(), on_saved=lambda: calls.append(1))
    with TestClient(app) as client:
        assert client.post("/api/leads", json={"email": "nope"}).status_code == 422
        assert client.post("/api/submissions", json=body("ab", "a@example.com")).status_code == 422

    assert calls == []


def test_a_failing_on_saved_hook_never_fails_the_request(settings, helpers, caplog):
    def on_saved():
        raise RuntimeError("zz-hook-sentinel")

    app = create_app(settings, helpers.FakeRunner(), on_saved=on_saved)
    with caplog.at_level(logging.WARNING, logger="bazaar_web.app"), TestClient(app) as client:
        lead = client.post("/api/leads", json={"email": "ada@example.com"})
        sub = client.post("/api/submissions", json=body("ada-bot", "ada@example.com"))

    assert (lead.status_code, sub.status_code) == (204, 201)
    assert caplog.text.count("on_saved hook failed (RuntimeError)") == 2
    assert "zz-hook-sentinel" not in caplog.text
