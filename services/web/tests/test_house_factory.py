"""v8 F2: the house lane runs strategist proposals, never repeats instructions, and waits on
any failure (there is no catalog to fall back to)."""

import dataclasses
import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime

import bazaar_web.worker
import httpx
import pytest
from bazaar_web.app import STATIC, create_app
from bazaar_web.bundle import BundleSource
from bazaar_web.house import check_proposal, normalize
from bazaar_web.settings import Settings
from bazaar_web.store import OPERATOR_CONTACT_EMAIL, Contact, Store
from bazaar_web.worker import expected_run_dir
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}
SINCE = datetime(2026, 10, 11, 20, 14, tzinfo=UTC)
UNIVERSE = [
    {"symbol": "KO", "name": "Coca-Cola", "sector": "Consumer Staples"},
    {"symbol": "MSFT", "name": "Microsoft", "sector": "Information Technology"},
    {"symbol": "BRK.B", "name": "Berkshire Hathaway", "sector": "Financials"},
]
ALLOWED = {"KO", "MSFT", "BRK.B", "SPY"}


def bundle_body(universe=UNIVERSE):
    body = {
        "bundle_id": "demo-bundle-v2", "bars": "sp-bars-v2", "news": "n", "filings": "f",
        "start_date": "2026-09-28", "end_date": "2026-10-09",
        "sessions": ["2026-09-28", "2026-10-09"], "incomplete_sessions": [],
        "universe_name": "S&P 500", "benchmark": "SPY",
    }  # fmt: skip
    return body | ({"universe": universe} if universe is not None else {})


def market(body):
    def make(market_url, bundle_id, **kwargs):
        reply = httpx.Response(404) if body is None else httpx.Response(200, json=body)
        kwargs.setdefault("transport", httpx.MockTransport(lambda request: reply))
        return BundleSource(market_url, bundle_id, **kwargs)

    return make


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_IDLE_S", 0.02)
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_FAIL_BACKOFF_S", 0.05)
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_FAIL_BACKOFF_MAX_S", 3.0)
    monkeypatch.setattr("bazaar_web.app.BundleSource", market(bundle_body()))


@pytest.fixture
def lane(settings):
    return dataclasses.replace(
        settings, house_enabled=True, house_concurrent=2, house_since=SINCE,
        house_propose_interval_s=0, data_bundle="demo-bundle-v2",
    )  # fmt: skip


class AutoRunner:
    """Finishes each run at once; records names and instructions."""

    def __init__(self, helpers):
        self.helpers = helpers
        self.started = []

    def __call__(self, *, submission_id, name, instructions, runs_dir, **_):
        self.started.append((name, instructions))
        return self.helpers.write_run(
            runs_dir, expected_run_dir(runs_dir, submission_id).name,
            policy_ref=f"submission-{name}", period_return="0.0100",
        )  # fmt: skip


def status(client):
    return client.get("/api/admin/house", headers=ADMIN).json()


def proposal(helpers, name="g1-ko-hold", text="Put $6,000 into KO on day 1 and hold it.", **kw):
    return helpers.FakeProposal(name, text, **kw)


def human(store, name, instructions, *, email="person@example.com"):
    return store.create(
        name=name, handle=None, instructions=instructions, ip_hash="h", max_queue=30,
        max_per_day=150, max_per_ip_hour=5, contact=Contact(email=email),
    )  # fmt: skip


# ---------- checks and dedup ----------


def test_normalize_folds_case_whitespace_and_edge_punctuation():
    assert (
        normalize("  Buy KO\n on   day 1.  ") == normalize("buy ko on day 1") == "buy ko on day 1"
    )
    assert normalize("Buy KO, then MSFT!") == "buy ko, then msft"  # inner punctuation stays


@pytest.mark.parametrize(
    ("name", "text", "parents", "reason"),
    [
        ("G1-Upper", "Put $6,000 into KO on day 1 and hold it.", [], "name"),
        ("g1", "Put $6,000 into KO on day 1 and hold it.", [], "name"),
        ("g1-" + "x" * 38, "Put $6,000 into KO on day 1 and hold it.", [], "name"),
        ("g2-wrong-gen", "Put $6,000 into KO on day 1 and hold it.", [], "generation prefix"),
        ("ko-hold", "Put $6,000 into KO on day 1 and hold it.", [], "generation prefix"),
        ("g1-short", "Buy KO and hold it.", [], "length"),  # 19 characters
        ("g1-long", "Buy KO. " * 76, [], "length"),  # 608 characters
        (
            "g1-amzn",
            "Put $6,000 into AMZN on day 1 and hold it.",
            [],
            "ticker outside the universe",
        ),
        ("g1-orphan", "Put $6,000 into KO on day 1 and hold it.", ["nobody"], "unknown parent"),
        ("g1-ok", "Put $6,000 into BRK.B and SPY on day 1; I hold no ETF.", ["parent-a"], None),
    ],
)
def test_each_proposal_is_checked(helpers, name, text, parents, reason):
    found = check_proposal(
        helpers.FakeProposal(name, text, parents=parents),
        generation=1, allowed=ALLOWED, parents={"parent-a"},
    )  # fmt: skip
    assert found == reason


def test_no_instructions_are_queued_twice(tmp_path, helpers):
    store = Store(tmp_path / "web.sqlite3")
    human(store, "attendee", "Put $6,000 into MSFT on day 1 and hold it.")
    batch = [
        proposal(helpers),
        proposal(helpers, "g1-same-again", "  put $6,000 into KO on day 1   and hold it  "),
        proposal(helpers, "g1-copies-human", "put $6,000 into MSFT on day 1 and hold it!"),
        proposal(helpers, "attendee", "Put $3,000 into KO on day 2."),  # taken (and no prefix)
        proposal(helpers, "g1-bad", "Buy AMZN on day 1 and hold to the end."),
    ]

    first = store.add_proposals(batch, generation=1, allowed=ALLOWED, parents=[])
    again = store.add_proposals(
        [proposal(helpers, "g1-new-name")], generation=1, allowed=ALLOWED, parents=[]
    )

    assert first == {
        "added": 1, "duplicate instructions": 2, "generation prefix": 1,
        "ticker outside the universe": 1,
    }  # fmt: skip
    assert again == {"duplicate instructions": 1}  # an earlier proposal's text
    assert store.unused_proposals() == 1
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(  # the UNIQUE index backs the check up
            "INSERT INTO house_proposals (name, instructions, normalized_instructions, parents, "
            "generation, created_at) VALUES ('g1-x', 'x', ?, '[]', 1, 't')",
            (normalize("Put $6,000 into KO on day 1 and hold it."),),
        )
    conn.close()


def test_a_proposal_taken_after_it_was_queued_is_skipped_cleanly(tmp_path, helpers):
    store = Store(tmp_path / "web.sqlite3")
    batch = [
        proposal(helpers, "g1-name-taken", "Put $1,000 into KO on day 1 and hold it."),
        proposal(helpers, "g1-text-taken", "Put $2,000 into KO on day 1 and hold it."),
        proposal(helpers, "g1-free", "Put $3,000 into KO on day 1 and hold it."),
    ]
    store.add_proposals(batch, generation=1, allowed=ALLOWED, parents=[])
    human(store, "g1-name-taken", "Something else entirely, held all week.")
    human(store, "copycat", "put $2,000 into KO on day 1 and hold it")

    created = store.create_house(None)

    assert created is not None and created[1] == "g1-free"
    assert store.unused_proposals() == 0  # both taken ones are used up, never retried
    assert store.create_house(None) is None


def test_a_unique_conflict_at_insert_skips_only_that_proposal(tmp_path, helpers):
    store = Store(tmp_path / "web.sqlite3")
    with sqlite3.connect(store.path) as conn:  # a conflict the checks before it did not see
        conn.execute(
            "CREATE TRIGGER clash BEFORE INSERT ON house_proposals WHEN NEW.name = 'g1-b' "
            "BEGIN SELECT RAISE(ABORT, 'UNIQUE constraint failed'); END"
        )
    conn.close()
    batch = [
        proposal(helpers, f"g1-{x}", f"Put ${n},000 into KO on day 1 and hold it.")
        for n, x in enumerate("abc", 1)
    ]

    outcome = store.add_proposals(batch, generation=1, allowed=ALLOWED, parents=[])

    assert outcome == {"added": 2, "conflict": 1}
    assert store.unused_proposals() == 2


# ---------- the lane ----------


def test_the_lane_asks_with_the_agreed_arguments_and_runs_the_proposals(lane, helpers, monkeypatch):
    fake = helpers.FakeStrategist(names=["parent-a", "parent-b", "baseline-cash-only"])
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    store = Store(lane.web_db)
    human(store, "parent-a", "Hold MSFT all week, whatever happens.")
    human(store, "smoke", "Operator smoke test: buy KO once.", email=OPERATOR_CONTACT_EMAIL)
    hidden = human(store, "hidden-dup", "A hidden duplicate: buy KO each day.")
    store.hide(hidden)
    runner = AutoRunner(helpers)
    with TestClient(create_app(lane, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) >= 4)
        client.post("/api/admin/house/pause", headers=ADMIN)
        state = status(client)

    (kind, brief), (kind2, propose) = fake.calls[:2]
    assert (kind, kind2) == ("brief", "propose")
    assert brief == {
        "bundle_id": "demo-bundle-v2", "since": SINCE, "environment": "production",
        "exclude_names": frozenset({"smoke", "hidden-dup"}), "data_version": "demo-bundle-v2",
    }  # fmt: skip
    assert propose["k"] == 6 and propose["generation"] == 1
    # Only brief names, and only those with a visible row (the baseline has none here).
    assert propose["instructions_by_name"] == {"parent-a": "Hold MSFT all week, whatever happens."}
    assert normalize("A hidden duplicate: buy KO each day.") in propose["existing_instructions"]
    assert normalize("Hold MSFT all week, whatever happens.") in propose["existing_instructions"]
    assert (propose["universe"], propose["benchmark"]) == (UNIVERSE, "SPY")
    house = [n for n, _ in runner.started if n.startswith("g")]
    assert house == fake.proposed[: len(house)] and len(house) >= 3
    assert state["generation"] >= 1 and state["state"] is None
    assert state["last_propose_at"] is not None and state["last_error_class"] is None


def test_generation_and_parents_are_kept_and_shown(lane, helpers, monkeypatch):
    fake = helpers.FakeStrategist()
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    runner = AutoRunner(helpers)
    config = dataclasses.replace(lane, house_concurrent=1)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) >= 7)  # into the second batch
        client.post("/api/admin/house/pause", headers=ADMIN)
        helpers.wait_for(lambda: status(client)["running"] == 0)
        rows = client.get("/api/board").json()["rows"]

    with sqlite3.connect(config.web_db) as conn:
        stored = conn.execute(
            "SELECT name, generation, parents FROM submissions WHERE lane = 'house' ORDER BY rowid"
        ).fetchall()
    conn.close()
    assert [g for _, g, _ in stored[:7]] == [1] * 6 + [2]  # one past the highest generation
    assert {p for _, _, p in stored} == {json.dumps(["parent-a"])}
    assert all(n.startswith(f"g{g}-") for n, g, _ in stored)
    assert {r["name"]: r["generation"] for r in rows if r["lane"] == "house"} == {
        n: g for n, g, _ in stored
    }
    batches = [c for c, _ in fake.calls].count("propose")
    assert batches >= 2 and max(g for _, g, _ in stored) <= batches  # one generation a batch


def test_the_interval_and_the_waiting_proposals_limit_calls(lane, helpers, monkeypatch):
    fake = helpers.FakeStrategist()
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    runner = AutoRunner(helpers)
    config = dataclasses.replace(lane, house_propose_interval_s=300)
    with TestClient(create_app(config, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) == 6)
        time.sleep(0.3)  # many idle rounds with no proposal left
        state = status(client)

    assert len(runner.started) == 6  # the batch, then a wait for the interval
    assert [c for c, _ in fake.calls] == ["brief", "propose"]
    assert state["proposals_waiting"] == 0


class GatedRunner(AutoRunner):
    """Holds each run until gate is set (conftest's drain sets it at teardown)."""

    def __init__(self, helpers):
        super().__init__(helpers)
        self.gate = threading.Event()

    def __call__(self, **kwargs):
        self.gate.wait(timeout=10)
        return super().__call__(**kwargs)


def test_no_call_while_enough_proposals_wait(lane, helpers, monkeypatch):
    fake = helpers.FakeStrategist()
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    store = Store(lane.web_db)
    batch = [proposal(helpers, f"g1-w{i}", f"Put ${i},000 into KO on day 1 and hold it.")
             for i in range(1, 5)]  # fmt: skip
    store.add_proposals(batch, generation=1, allowed=ALLOWED, parents=[])
    runner = GatedRunner(helpers)
    with TestClient(create_app(lane, runner)) as client:
        helpers.wait_for(lambda: status(client)["running"] == 2)
        time.sleep(0.2)  # many idle rounds
        state = status(client)
        client.post("/api/admin/house/pause", headers=ADMIN)
        runner.gate.set()

    assert state["proposals_waiting"] == 2  # as many as the slots: enough, so no call
    assert fake.calls == []


class Boom(Exception):
    """Stands in for StrategistError."""


@pytest.mark.parametrize("failure", ["error", "import", "no valid proposals"])
def test_any_strategist_failure_means_wait_with_backoff(
    lane, helpers, monkeypatch, caplog, failure
):
    fake = helpers.FakeStrategist(
        fail=Boom("Logfire said: secret-ish details") if failure == "error" else None
    )
    if failure == "no valid proposals":
        fake.batches = [[proposal(helpers, "no-prefix")]] * 50

    def get():
        if failure == "import":
            raise ImportError("no bazaar_agent here")
        return fake

    monkeypatch.setattr(bazaar_web.worker, "import_strategist", get)
    runner = AutoRunner(helpers)
    with caplog.at_level(logging.INFO), TestClient(create_app(lane, runner)) as client:
        helpers.wait_for(lambda: status(client)["state"] == "waiting for strategist")
        time.sleep(0.6)
        state = status(client)
        board = client.get("/api/board").json()["rows"]

    assert runner.started == [] and board == []  # nothing ran: no catalog, no fallback
    expected = {"error": "Boom", "import": "ImportError", "no valid proposals": "NoProposals"}
    assert state["last_error_class"] == expected[failure]
    assert state["strategist_retry_at"] is not None
    calls = [c for c, _ in fake.calls].count("brief")
    if failure != "import":
        assert 1 <= calls <= 6  # 0.05 s doubling: not a hot loop
    assert "secret-ish" not in caplog.text and "no-prefix" not in caplog.text


def test_resume_asks_the_strategist_again_at_once(lane, helpers, monkeypatch):
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_FAIL_BACKOFF_S", 60.0)
    fake = helpers.FakeStrategist(fail=Boom("down"))
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    runner = AutoRunner(helpers)
    with TestClient(create_app(lane, runner)) as client:
        helpers.wait_for(lambda: status(client)["state"] == "waiting for strategist")
        fake.fail = None
        client.post("/api/admin/house/resume", headers=ADMIN)
        helpers.wait_for(lambda: len(runner.started) >= 1)
        client.post("/api/admin/house/pause", headers=ADMIN)

    assert runner.started[0][0] == fake.proposed[0]


@pytest.mark.parametrize(
    ("change", "body", "waiting"),
    [
        ({"house_since": None}, bundle_body(), "factory not configured"),
        ({}, bundle_body(universe=None), "no universe in the bundle"),  # demo-bundle-v1
        ({}, None, "waiting for bundle"),
    ],
)
def test_without_since_or_a_universe_the_lane_waits(
    lane, helpers, monkeypatch, change, body, waiting
):
    monkeypatch.setattr("bazaar_web.app.BundleSource", market(body))
    fake = helpers.FakeStrategist()
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    runner = AutoRunner(helpers)
    with TestClient(create_app(dataclasses.replace(lane, **change), runner)) as client:
        helpers.wait_for(lambda: status(client)["state"] == waiting)
        time.sleep(0.2)
        state = status(client)

    assert runner.started == [] and fake.calls == []
    assert state["total_started"] == 0
    assert state["waiting_for_bundle"] is (waiting == "waiting for bundle")


def test_waiting_proposals_survive_a_restart_in_order(lane, helpers, monkeypatch):
    fake = helpers.FakeStrategist()
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    store = Store(lane.web_db)
    batch = [proposal(helpers, f"g1-r{i}", f"Put ${i},000 into KO on day 1 and hold it.")
             for i in range(1, 9)]  # fmt: skip
    store.add_proposals(batch, generation=1, allowed=ALLOWED, parents=[])
    started = []
    for _ in range(2):
        runner = AutoRunner(helpers)
        with TestClient(create_app(lane, runner)) as client:
            helpers.wait_for(lambda r=runner: len(r.started) >= 3)
            client.post("/api/admin/house/pause", headers=ADMIN)
            helpers.wait_for(lambda c=client: status(c)["running"] == 0)
        started += [n for n, _ in runner.started]
        Store(lane.web_db).set_house_paused(False)

    queued = [f"g1-r{i}" for i in range(1, 9)]
    assert len(started) == len(set(started))  # nothing used twice across the restart
    # The waiting proposals go first, in order (two slots may start a pair either way), and
    # the strategist's only once they run low.
    first = [n for n in started if n in queued]
    assert sorted(first) == queued[: len(first)] and len(first) >= 6
    assert started[: len(first)] == first or set(started[: len(first)]) == set(first)


def test_the_propose_span_carries_generation_and_parents(lane, helpers, monkeypatch, capfire):
    fake = helpers.FakeStrategist()
    monkeypatch.setattr(bazaar_web.worker, "import_strategist", lambda: fake)
    runner = AutoRunner(helpers)
    with TestClient(create_app(lane, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) >= 1)
        client.post("/api/admin/house/pause", headers=ADMIN)

    spans = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "house propose"]
    attributes = spans[0]["attributes"]
    assert attributes["bazaar.generation"] == 1
    parents = attributes["bazaar.parents"]
    assert (json.loads(parents) if isinstance(parents, str) else list(parents)) == ["parent-a"]
    assert attributes["bazaar.proposals_added"] == 6
    assert "MSFT" not in json.dumps(attributes, default=str)  # names only, never instructions


# ---------- migration, settings, page ----------


def test_an_old_database_gets_the_factory_columns_and_table(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, lane TEXT DEFAULT 'human');"
            "CREATE TABLE house_state (id INTEGER PRIMARY KEY CHECK (id = 1), cursor INTEGER NOT "
            "NULL, cycle INTEGER NOT NULL, paused INTEGER NOT NULL, total_started INTEGER NOT NULL);"
            "INSERT INTO house_state VALUES (1, 57, 18, 0, 2253);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at, lane) "
            "VALUES ('old', 'house-ko-hold-18', 'Buy KO.', 'house', 'scored', 't', 'house');"
        )
    conn.close()

    store = Store(path)
    Store(path)  # idempotent

    row = store.get("old")
    assert (row["generation"], row["parents"]) == (None, None)  # catalog-era: no generation
    state = store.house_state()
    assert (state["cursor"], state["cycle"], state["total_started"]) == (57, 18, 2253)  # kept
    assert store.factory_state() == {"generation": None, "proposals_waiting": 0}
    assert store.factory_inputs()[2] == 1


def test_factory_settings_from_env(monkeypatch):
    monkeypatch.delenv("BAZAAR_HOUSE_SINCE", raising=False)
    monkeypatch.delenv("BAZAAR_HOUSE_PROPOSE_INTERVAL_S", raising=False)
    default = Settings.from_env()
    assert (default.house_since, default.house_propose_interval_s) == (None, 300)
    monkeypatch.setenv("BAZAAR_HOUSE_SINCE", "2026-10-11T16:14:00-04:00")
    monkeypatch.setenv("BAZAAR_HOUSE_PROPOSE_INTERVAL_S", "120")
    on = Settings.from_env()
    assert (on.house_since, on.house_propose_interval_s) == (SINCE, 120)
    monkeypatch.setenv("BAZAAR_HOUSE_SINCE", "2026-10-11T20:14:00Z")
    assert Settings.from_env().house_since == SINCE


@pytest.mark.parametrize(
    ("name", "raw"),
    [("BAZAAR_HOUSE_SINCE", "2026-10-11T20:14:00"), ("BAZAAR_HOUSE_SINCE", "yesterday"),
     ("BAZAAR_HOUSE_PROPOSE_INTERVAL_S", "0")],
)  # fmt: skip
def test_bad_factory_settings_are_refused(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_the_tag_shows_the_generation_as_text():
    page = (STATIC / "board.html").read_text()
    assert "if (Number.isInteger(gen) && gen > 0) chips.push(`G${gen}`);" in page
