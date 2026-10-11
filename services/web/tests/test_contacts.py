import csv
import dataclasses
import io
import json
import logging
import sqlite3

import pytest
from bazaar_web.app import create_app
from bazaar_web.store import OPERATOR_CONTACT_EMAIL, Contact, Store
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}
CONTACT = {"email": "alice@example.com", "company": "Example Co", "role": "Engineer"}
SENTINELS = ("zz-sentinel@example.com", "ZZCOMPANYSENTINEL", "ZZROLESENTINEL")
SENTINEL_CONTACT = dict(zip(("email", "company", "role"), SENTINELS, strict=True))


def body(name="alice-bot", **changes):
    base = {"name": name, "handle": None, "instructions": "Buy KO on dips, hold MSFT."}
    return base | CONTACT | changes


def status(client, submission_id):
    return client.get(f"/api/submissions/{submission_id}").json()


def contacts_csv(client):
    response = client.get("/api/admin/contacts", headers=ADMIN)
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.text)))


# ---------- validation ----------


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("  Alice@Example.COM ", "alice@example.com"),
        ("a.b+tag@mail.example.co.uk", "a.b+tag@mail.example.co.uk"),
        ("x@" + "d" * 240 + ".com", "x@" + "d" * 240 + ".com"),  # 247 chars
    ],
)
def test_a_valid_email_is_stripped_and_lowercased(settings, helpers, given, stored):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        sid = client.post("/api/submissions", json=body(email=given)).json()["id"]

    assert Store(settings.web_db).get(sid)["email"] == stored


@pytest.mark.parametrize(
    "email",
    [
        "", "   ", "alice", "alice@", "@example.com", "alice@example", "a@b@example.com",
        "al ice@example.com", "alice@.example.com", "alice@example.com.", "alice@example..com",
        "alice@exa mple.com", "x@" + "d" * 250 + ".com",
    ],
)  # fmt: skip
def test_a_bad_email_is_a_422_with_the_form_message(settings, helpers, email):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.post("/api/submissions", json=body(email=email))

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["loc"], error["msg"]) == (["body", "email"], "Enter a valid email address")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("company", "", "Enter your company (up to 100 characters)"),
        ("company", "   ", "Enter your company (up to 100 characters)"),
        ("company", "c" * 101, "Enter your company (up to 100 characters)"),
        ("role", "", "Enter your role (up to 100 characters)"),
        ("role", "r" * 101, "Enter your role (up to 100 characters)"),
    ],
)
def test_company_and_role_are_1_to_100_characters(settings, helpers, field, value, message):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.post("/api/submissions", json=body(**{field: value}))

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["loc"], error["msg"]) == (["body", field], message)


def test_company_and_role_are_stripped_and_100_characters_fit(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        sid = client.post(
            "/api/submissions", json=body(company="  " + "c" * 100 + " ", role=" Quant ")
        ).json()["id"]

    row = Store(settings.web_db).get(sid)
    assert (row["company"], row["role"]) == ("c" * 100, "Quant")


@pytest.mark.parametrize("field", ["email", "company", "role"])
def test_each_contact_field_is_required(settings, helpers, field):
    payload = body()
    del payload[field]
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        response = client.post("/api/submissions", json=payload)

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert (error["loc"], error["msg"]) == (["body", field], "Field required")
    assert set(error) == {"type", "loc", "msg"}  # no "input" echo of the whole body


# ---------- storage ----------


def test_migration_adds_nullable_contact_columns(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, latest_value TEXT, trace_context TEXT, "
            "rerun_of TEXT);"
            "CREATE TABLE events (t TEXT NOT NULL, text TEXT NOT NULL, submission_id TEXT);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'scored', '2026-10-08T14:00:00+00:00');"
        )
    conn.close()

    store = Store(path)
    Store(path)  # idempotent

    old = store.get("old1")
    assert (old["email"], old["company"], old["role"]) == (None, None, None)
    assert store.contacts() == []


def test_a_rerun_copies_the_contact_details(seeded, helpers):
    with TestClient(create_app(seeded, helpers.FakeRunner())) as client:
        sid = client.post("/api/submissions", json=body()).json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        new_id = client.post(f"/api/admin/submissions/{sid}/rerun", headers=ADMIN).json()["id"]

    new = Store(seeded.web_db).get(new_id)
    assert (new["email"], new["company"], new["role"]) == (
        "alice@example.com",
        "Example Co",
        "Engineer",
    )


def test_the_contact_repr_hides_personal_data():
    assert "alice" not in repr(Contact(**CONTACT))


# ---------- the admin CSV ----------


def test_contacts_csv_needs_the_admin_token(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        assert client.get("/api/admin/contacts").status_code == 403
        wrong = client.get("/api/admin/contacts", headers={"X-Bazaar-Admin-Token": "nope"})
        assert wrong.status_code == 403
    unset = dataclasses.replace(settings, admin_token=None)
    with TestClient(create_app(unset, helpers.FakeRunner())) as client:
        assert client.get("/api/admin/contacts", headers=ADMIN).status_code == 404


def test_contacts_csv_rows_match_the_board_and_leave_out_hidden_and_contactless(seeded, helpers):
    runner = helpers.FakeRunner(returns={"winner-bot": "0.0450", "second-bot": "0.0200"})
    clock = helpers.Clock()
    with TestClient(create_app(seeded, runner, now=clock)) as client:
        ids = {}
        for name in ("winner-bot", "second-bot", "hidden-bot"):
            ids[name] = client.post("/api/submissions", json=body(name)).json()["id"]
        for sid in ids.values():
            helpers.wait_for(lambda s=sid: status(client, s)["status"] == "scored")
        client.post(f"/api/admin/submissions/{ids['hidden-bot']}/hide", headers=ADMIN)
        runner.gate.clear()
        waiting = client.post("/api/submissions", json=body("waiting-bot")).json()["id"]
        board = {r["id"]: r for r in client.get("/api/board").json()["rows"]}
        response = client.get("/api/admin/contacts", headers=ADMIN)
        rows = list(csv.DictReader(io.StringIO(response.text)))
        runner.gate.set()

    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="bazaar-contacts-20261008T140000Z.csv"'
    )
    assert response.text.splitlines()[0] == (
        "name,handle,email,company,role,created_at,return_pct,rank"
    )
    # The seeded runs (no contact details) and the hidden row are not in it.
    assert [r["name"] for r in rows] == ["winner-bot", "second-bot", "waiting-bot"]
    for row in rows[:2]:
        on_board = board[ids[row["name"]]]
        assert float(row["return_pct"]) == on_board["return_pct"]
        assert int(row["rank"]) == on_board["rank"]
    assert (rows[2]["return_pct"], rows[2]["rank"]) == ("", "")  # not scored yet
    assert rows[0]["email"] == "alice@example.com" and waiting in board


def test_contacts_csv_leaves_out_operator_and_contactless_rows(settings, helpers):
    store = Store(settings.web_db)
    rows = [
        ("real-bot", Contact(**CONTACT)),
        ("smoke-bot", Contact(email=OPERATOR_CONTACT_EMAIL, company="Bazaar operators", role="x")),
        ("shouty-ops", Contact(email=OPERATOR_CONTACT_EMAIL.upper(), company="Ops", role="x")),
        ("no-contact", None),
    ]
    for name, contact in rows:
        store.create(
            name=name, handle=None, instructions="Buy KO on dips, hold MSFT.", ip_hash="h",
            max_queue=30, max_per_day=150, max_per_ip_hour=5, contact=contact,
        )  # fmt: skip
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        listed = [row["name"] for row in contacts_csv(client)]

    assert listed == ["real-bot"]


def test_the_operator_scripts_send_the_operator_address():
    from pathlib import Path

    scripts = Path(__file__).parents[3] / "scripts"
    for name in ("live_smoke.py", "house_feeder.py", "house_strategies.py"):
        assert f'"email": "{OPERATOR_CONTACT_EMAIL}"' in (scripts / name).read_text(), name


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
def test_contacts_csv_defuses_formulas(settings, helpers, start):
    store = Store(settings.web_db)
    store.create(
        name="-formula-bot" if start == "-" else "formula-bot",
        handle=f"{start}cmd|' /C calc'!A0",
        instructions="Buy KO on dips, hold MSFT.",
        ip_hash="h",
        max_queue=30,
        max_per_day=150,
        max_per_ip_hour=5,
        contact=Contact(
            email=f"{start}x@example.com", company=f"{start}SUM(A1)", role=f"{start}HYPERLINK()"
        ),
    )
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        [row] = contacts_csv(client)

    for column in ("handle", "email", "company", "role"):
        assert row[column].startswith("'" + start), (column, row[column])
    if start == "-":
        assert row["name"] == "'-formula-bot"


def test_contacts_csv_is_not_traced(settings, helpers, capfire):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        capfire.exporter.clear()
        client.get("/api/admin/contacts", headers=ADMIN)

    assert capfire.exporter.exported_spans_as_dict() == []


# ---------- the leak test ----------


class Spy:
    """Records every keyword the worker passes to run_submission, then runs the fake."""

    def __init__(self, fake):
        self.fake = fake
        self.kwargs = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        return self.fake(**kwargs)


def test_contact_details_reach_no_span_log_response_runner_or_run_file(
    seeded, helpers, capfire, caplog, tmp_path
):
    """The only way out of the web DB is the admin CSV."""
    caplog.set_level(logging.DEBUG)
    config = dataclasses.replace(seeded, max_queue=2, max_per_ip_hour=3, max_per_day=4)
    spy = Spy(helpers.FakeRunner(fail={"failing-bot"}))
    responses = []

    def record(response):
        responses.append(response)
        return response

    with TestClient(create_app(config, spy)) as client:
        sid = record(client.post("/api/submissions", json=body("sentinel-bot", **SENTINEL_CONTACT)))
        sid = sid.json()["id"]
        helpers.wait_for(lambda: status(client, sid)["status"] == "scored")
        bad = record(client.post("/api/submissions", json=body("failing-bot", **SENTINEL_CONTACT)))
        helpers.wait_for(lambda: status(client, bad.json()["id"])["status"] == "failed")
        # Every error path of the submit route, carrying the sentinels.
        errors = [
            body("sentinel-bot", **SENTINEL_CONTACT),  # name taken
            body("x-bot", **(SENTINEL_CONTACT | {"email": "zz-sentinel@@example.com"})),
            body("x-bot", **(SENTINEL_CONTACT | {"company": SENTINELS[1] * 10})),
            body("x-bot", **(SENTINEL_CONTACT | {"role": SENTINELS[2] * 10})),
            {k: v for k, v in body("x-bot", **SENTINEL_CONTACT).items() if k != "email"},
            body("X BOT", **SENTINEL_CONTACT),  # bad name, valid contacts
            body("x-bot", instructions="short", **SENTINEL_CONTACT),
        ]
        for payload in errors:
            response = record(client.post("/api/submissions", json=payload))
            assert response.status_code == 422, response.text
        # The caps: three from one address fill its hourly cap; a fourth from another address
        # fills the day; the queue cap is below, in a second app with the runner held.
        assert record(client.post("/api/submissions", json=body("cap-0", **SENTINEL_CONTACT)))
        caps = [
            client.post("/api/submissions", json=body("ip-capped", **SENTINEL_CONTACT)),
            client.post(
                "/api/submissions",
                json=body("day-four", **SENTINEL_CONTACT),
                headers={"X-Forwarded-For": "9.9.9.9"},
            ),
            client.post(
                "/api/submissions",
                json=body("day-capped", **SENTINEL_CONTACT),
                headers={"X-Forwarded-For": "8.8.8.8"},
            ),
        ]
        responses += caps
        assert [r.status_code for r in caps] == [429, 201, 429]
        for path in ("/", "/submit", "/api/board", "/api/tickers", f"/api/submissions/{sid}"):
            record(client.get(path))
        record(client.get("/api/admin/whoami", headers=ADMIN))
        record(client.post(f"/api/admin/submissions/{sid}/rerun", headers=ADMIN))
        exported = contacts_csv(client)

    held = helpers.FakeRunner()
    held.gate.clear()
    queue = dataclasses.replace(seeded, web_db=tmp_path / "q.sqlite3", max_queue=1)
    with TestClient(create_app(queue, Spy(held))) as client:
        first = record(client.post("/api/submissions", json=body("q-one", **SENTINEL_CONTACT)))
        full = record(client.post("/api/submissions", json=body("q-two", **SENTINEL_CONTACT)))
        held.gate.set()
        helpers.wait_for(lambda: status(client, first.json()["id"])["status"] == "scored")
    assert (first.status_code, full.status_code) == (201, 429)
    assert "queue is full" in full.json()["detail"]

    assert any(r["email"] == SENTINELS[0] for r in exported)  # stored, and only there
    names = {span["name"] for span in capfire.exporter.exported_spans_as_dict()}
    assert "POST /api/submissions" in names and len(names) > 3  # the spans were really checked
    texts = {
        "spans": json.dumps(capfire.exporter.exported_spans_as_dict(), default=str),
        "logs": "\n".join(f"{r.name} {r.getMessage()} {r.args}" for r in caplog.records),
        "responses": "\n".join(f"{r.status_code} {r.text} {dict(r.headers)}" for r in responses),
        "runner kwargs": repr(spy.kwargs),
        "run files": "\n".join(
            p.read_text(errors="replace") for p in seeded.runs_dir.rglob("*") if p.is_file()
        ),
    }
    for where, text in texts.items():
        for sentinel in SENTINELS:
            assert sentinel.lower() not in text.lower(), (sentinel, where)
    assert all(set(k) == set(spy.kwargs[0]) for k in spy.kwargs)
    assert not {"email", "company", "role"} & set(spy.kwargs[0])
