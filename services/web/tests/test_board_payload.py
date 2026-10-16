"""/api/board at scale: per-run parse cache, the trimmed payload, ranks over every row."""

import dataclasses
import json
import os
import shutil
import sqlite3
import time

import bazaar_web.board
import pytest
from bazaar_web.app import STATIC, create_app
from bazaar_web.board import BoardSource
from bazaar_web.store import Store
from fastapi.testclient import TestClient

RUNS = 3000
BASELINES = ["baseline-buy-and-hold", "baseline-cash-only", "baseline-equal-weight"]


def ret(i: int) -> float:
    """A distinct return per run, spread so ranks are known: higher i, lower return."""
    return round(0.09 - i * 0.00005, 6)


_BUILT = {}


@pytest.fixture
def big_runs(tmp_path_factory, helpers):
    """3,000 synthetic run dirs (10 marks each), written once for the module."""
    if "runs" not in _BUILT:
        runs = tmp_path_factory.mktemp("big") / "runs"
        runs.mkdir()
        for i in range(RUNS):
            policy = BASELINES[i % 3] if i in (2500, 2600, 2700) else f"agent-{i:04d}"
            marks = tuple(str(round(10000 * (1 + ret(i) * d / 10), 2)) for d in range(1, 11))
            helpers.write_run(
                runs, f"run-{i:04d}", policy_ref=policy, period_return=str(ret(i)), marks=marks
            )
        for path in runs.rglob("*"):  # every test hard-links these: a write through one fails
            if path.is_file():
                path.chmod(0o444)
        _BUILT["runs"] = runs
    return _BUILT["runs"]


@pytest.fixture
def big(settings, big_runs, tmp_path):
    """Each test's own runs dir over the shared 3,000: its dirs are new, but its files are hard
    links (no new inodes; /tmp is an inode-capped tmpfs). Tests only add or remove run dirs
    here, never rewrite a file, so the links never change the shared copy."""
    runs = tmp_path / "runs"
    shutil.copytree(big_runs, runs, copy_function=os.link)
    return dataclasses.replace(settings, runs_dir=runs)


def link(config, run_id: str, name: str) -> str:
    """A scored web submission whose run is run_id."""
    store = Store(config.web_db)
    sid = store.create(
        name=name, handle=None, instructions="Buy KO on dips, hold MSFT.", ip_hash="h",
        max_queue=30, max_per_day=150, max_per_ip_hour=5,
    )  # fmt: skip
    with sqlite3.connect(config.web_db) as conn:
        conn.execute(
            "UPDATE submissions SET status = 'scored', run_dir = ? WHERE id = ?", (run_id, sid)
        )
    conn.close()
    return sid


def test_the_payload_is_small_and_a_warm_request_parses_nothing(big, helpers, monkeypatch):
    calls, reads = [], []
    real = bazaar_web.board.load_run
    monkeypatch.setattr(bazaar_web.board, "load_run", lambda path: calls.append(path) or real(path))
    real_history = bazaar_web.board.history
    monkeypatch.setattr(
        bazaar_web.board, "history", lambda path: reads.append(path) or real_history(path)
    )
    runner = helpers.FakeRunner()
    runner.gate.clear()
    with TestClient(create_app(big, runner)) as client:
        sid = client.post(
            "/api/submissions",
            json={"name": "live-bot", "handle": None, "instructions": "Buy KO on dips, hold MSFT.",
                  "email": "a@example.com", "company": "Co", "role": "Dev"},
        ).json()["id"]  # fmt: skip
        helpers.wait_for(lambda: client.get(f"/api/submissions/{sid}").json()["day"] == 10)
        cold = client.get("/api/board")
        cold_parsed, cold_read = len(calls), len(reads)
        # A rebuild, as after the 2 s TTL or a scored run: it lists every dir again.
        client.app.state.board.invalidate()
        start = time.perf_counter()
        warm = client.get("/api/board")
        warm_s = time.perf_counter() - start  # reported only; CI asserts no timing
        warm_parsed, warm_read = len(calls), len(reads)
        runner.gate.set()  # live-bot then scores, and its own new run dir is parsed

    body = warm.json()
    print(f"\n3,000 runs: {len(cold.content)} bytes, warm rebuild {warm_s:.3f} s (not asserted)")
    assert cold_parsed == RUNS and cold_read == RUNS  # each dir parsed once, by the first build
    # The warm rebuild, chart grouping included, parsed nothing and read no history.
    assert warm_parsed == cold_parsed and warm_read == cold_read
    assert sum(r["chart_members"] is not None for r in body["rows"]) == 10
    assert len(warm.content) < 100_000
    assert body["total"]["rows"] == RUNS + 1 and body["total"]["scored"] == RUNS
    scored = [r for r in body["rows"] if r["status"] == "scored"]
    top = [r for r in scored if r["kind"] != "baseline"]
    assert [r["rank"] for r in top[:100]] == list(range(1, 100 + 1))
    assert len(scored) == 100 + len(BASELINES)  # every baseline, though ranked > 100
    assert {r["name"] for r in scored if r["kind"] == "baseline"} == set(BASELINES)
    assert any(r["id"] == sid and r["status"] == "running" for r in body["rows"])


def test_history_is_sent_only_for_the_chart(big, helpers):
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        rows = client.get("/api/board").json()["rows"]

    # Neighbouring runs' values differ by $0.05 x day ($0.50 on day 10), so ranks 1-3 share a
    # line (all within $1 of rank 1), 4-6 the next, and so on: 10 lines, each "+2".
    reps = [r for r in rows if r["chart_members"] is not None]
    assert [(r["rank"], r["chart_members"]) for r in reps] == [(1 + 3 * i, 3) for i in range(10)]
    charted = {r["id"] for r in reps} | {r["id"] for r in rows if r["kind"] == "baseline"}
    assert {r["id"] for r in rows if r["history"] is not None} == charted
    assert len(charted) == 10 + len(BASELINES)  # 10 lines and the baselines; none running
    assert all(len(r["history"]) == 10 for r in rows if r["id"] in charted)


def test_ranks_are_over_every_row(big, helpers):
    # run-1499 is the 1,500th best agent, below two baselines (2500, 2600, 2700 are lower).
    sid = link(big, "run-1499", "deep-bot")
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        status = client.get(f"/api/submissions/{sid}").json()
        board = client.get("/api/board").json()["rows"]

    assert status["rank"] == 1500 and status["return_pct"] == pytest.approx(
        ret(1499) * 100, abs=0.05
    )
    # Ranked > 100 overall, but the Human tab's best: sent for that tab, with its overall rank.
    sent = next(r for r in board if r["id"] == sid)
    assert (sent["rank"], sent["lane_rank"]) == (1500, 1)
    assert {
        r["id"] for r in board if r["rank"] and r["rank"] > 100 and r["kind"] != "baseline"
    } == {sid}


def test_each_run_dir_is_parsed_once(big, helpers, monkeypatch):
    calls = []
    real = bazaar_web.board.load_run
    monkeypatch.setattr(bazaar_web.board, "load_run", lambda path: calls.append(path) or real(path))
    source = BoardSource(big.runs_dir, ttl=0)

    first, _ = source.load()
    assert len(calls) == RUNS
    again, _ = source.load()
    assert len(calls) == RUNS and again.ranked == first.ranked  # nothing re-parsed

    shutil.rmtree(big.runs_dir / "run-0000")  # the best run goes away
    helpers.write_run(big.runs_dir, "run-new", policy_ref="agent-new", period_return="0.5")
    board, histories = source.load()
    assert [p.name for p in calls[RUNS:]] == ["run-new"]  # only the new dir parsed
    assert board.ranked[0].run_id == "run-new" and "run-0000" not in histories
    assert len(board.ranked) == RUNS


def test_the_web_and_runner_default_to_one_bundle():
    """BAZAAR_DATA_BUNDLE unset: the board and the runs it launches still agree."""
    from bazaar_runner.bundle import DEMO_BUNDLE
    from bazaar_web.settings import DEFAULT_BUNDLE

    assert DEFAULT_BUNDLE == DEMO_BUNDLE


def test_the_board_ranks_only_runs_on_its_bundle(tmp_path, helpers):
    runs = tmp_path / "runs"
    runs.mkdir()
    # The old-bundle run sorts first, so without a bundle it anchors the board.
    helpers.write_run(runs, "run-a", policy_ref="agent-old", period_return="0.01")
    for name, ret in (("run-b", "0.02"), ("run-c", "0.03")):
        helpers.write_run(
            runs, name, policy_ref=f"agent-{name}", period_return=ret, data_version="demo-bundle-v2"
        )

    unfiltered, _ = BoardSource(runs, ttl=0).load()
    assert [e.run_id for e in unfiltered.ranked] == ["run-a"]
    assert {e.run_id for e in unfiltered.not_comparable} == {"run-b", "run-c"}

    board, histories = BoardSource(runs, ttl=0, bundle="demo-bundle-v2").load()
    assert [e.run_id for e in board.ranked] == ["run-c", "run-b"]
    assert board.not_comparable == () and set(histories) == {"run-b", "run-c"}


def test_the_app_board_follows_the_configured_bundle(settings, helpers):
    config = dataclasses.replace(settings, data_bundle="demo-bundle-v2")
    helpers.write_run(config.runs_dir, "run-old", policy_ref="agent-old", period_return="0.05")
    helpers.write_run(
        config.runs_dir, "run-new", policy_ref="agent-new", period_return="0.01",
        data_version="demo-bundle-v2",
    )  # fmt: skip
    with TestClient(create_app(config, helpers.FakeRunner())) as client:
        rows = client.get("/api/board").json()["rows"]

    assert [row["name"] for row in rows] == ["agent-new"]


def test_a_republished_dir_is_parsed_again(tmp_path, helpers):
    write_run = helpers.write_run
    runs = tmp_path / "runs"
    runs.mkdir()
    write_run(runs, "run-a", policy_ref="agent-a", period_return="0.01")
    source = BoardSource(runs, ttl=0)
    assert source.load()[0].ranked[0].period_return == helpers.Decimal("0.01")
    # The runner stages a run and renames it into place: a new dir, so a new inode.
    write_run(tmp_path, "staged", policy_ref="agent-a", period_return="0.02")
    shutil.rmtree(runs / "run-a")
    (tmp_path / "staged").rename(runs / "run-a")

    assert source.load()[0].ranked[0].period_return == helpers.Decimal("0.02")


def test_the_page_says_connecting_until_the_first_board():
    page = (STATIC / "board.html").read_text()
    assert (
        '<ol class="ar-list" id="list"><li class="empty" id="connecting">CONNECTING...</li></ol>'
        in page
    )
    # The first render clears any .empty item, so the connecting line goes with real rows.
    assert 'if (list.querySelector(".empty")) list.innerHTML = "";' in page


def test_the_payload_shape_is_json(big, helpers):
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        body = client.get("/api/board").json()
    assert json.loads(json.dumps(body)) == body
    assert set(body) == {"window", "rows", "total", "events", "factory"}


# ---------- tabs: each lane's top 15, per-tab chart lines, the factory state ----------

HOUSE_RUNS = range(300)  # the 300 best agents are the factory's
HUMAN_RUNS = range(1000, 1020)  # 20 humans, all ranked about 1,000th overall


def lanes(config, house=HOUSE_RUNS, human=HUMAN_RUNS):
    """Scored submissions in each lane for these run dirs."""
    Store(config.web_db)  # the schema
    rows = [
        (f"{lane}{i}", f"{lane}-{i:04d}", "Buy KO.", lane, "scored", "2026-10-11T00:00:00",
         f"run-{i:04d}", 0, lane)
        for lane, runs in (("house", house), ("human", human))
        for i in runs
    ]  # fmt: skip
    with sqlite3.connect(config.web_db) as conn:
        conn.executemany(
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at, "
            "run_dir, hidden, lane) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.close()


def test_each_tab_gets_its_top_15_and_a_warm_request_still_parses_nothing(
    big, helpers, monkeypatch
):
    lanes(big)
    calls, reads = [], []
    real = bazaar_web.board.load_run
    monkeypatch.setattr(bazaar_web.board, "load_run", lambda path: calls.append(path) or real(path))
    real_history = bazaar_web.board.history
    monkeypatch.setattr(
        bazaar_web.board, "history", lambda path: reads.append(path) or real_history(path)
    )
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        client.get("/api/board")
        cold_parsed, cold_read = len(calls), len(reads)
        client.app.state.board.invalidate()
        warm = client.get("/api/board")

    rows = warm.json()["rows"]
    assert len(calls) == cold_parsed and len(reads) == cold_read  # the warm rebuild read nothing
    assert len(warm.content) < 100_000
    # The overall top 100 are all factory rows; the humans, ranked ~1,000th, still fill the
    # Human tab: its top 15, ranked 1..15 within the lane, and no more.
    human = [r for r in rows if r["lane"] == "human"]
    assert [(r["name"], r["lane_rank"]) for r in human] == [
        (f"human-{i:04d}", n) for n, i in enumerate(HUMAN_RUNS[:15], start=1)
    ]
    assert all(r["rank"] > 1000 for r in human)
    house = [r for r in rows if r["lane"] == "house"]
    assert [r["lane_rank"] for r in house] == list(range(1, 101))  # the top 100, overall too
    assert [r["rank"] for r in house] == list(range(1, 101))


def test_rows_are_unique_by_id(big, helpers):
    lanes(big)  # the factory's top 15 are also the overall top 15
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        rows = client.get("/api/board").json()["rows"]

    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) == 100 + 15 + len(BASELINES)


def test_each_tab_has_its_own_chart_lines(big, helpers):
    lanes(big)
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        rows = client.get("/api/board").json()["rows"]

    def lines(tab):
        return [(r["name"], r["chart_tabs"][tab]) for r in rows if (r["chart_tabs"] or {}).get(tab)]

    # Neighbours share a line in threes (as in test_history_is_sent_only_for_the_chart).
    assert lines("both") == lines("factory") == [(f"house-{3 * i:04d}", 3) for i in range(10)]
    assert lines("human") == [(f"human-{1000 + 3 * i:04d}", 3) for i in range(5)]
    assert [r["name"] for r in rows if r["chart_members"]] == [n for n, _ in lines("both")]
    charted = {n for tab in ("both", "human", "factory") for n, _ in lines(tab)} | set(BASELINES)
    assert {r["name"] for r in rows if r["history"] is not None} == charted


def factory(config, *, enabled, paused=False, running=0):
    store = Store(config.web_db)
    store.set_house_paused(paused)
    with sqlite3.connect(config.web_db) as conn:
        conn.executemany(
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at, lane) "
            "VALUES (?, ?, 'Buy KO.', 'house', 'running', '2026-10-11T00:00:00', 'house')",
            [(f"r{i}", f"running-{i}") for i in range(running)],
        )
    conn.close()
    source = BoardSource(config.runs_dir)
    return bazaar_web.board.build_board(source, store, house_enabled=enabled)["factory"]


@pytest.mark.parametrize(
    ("enabled", "paused", "running", "expected"),
    [
        (False, False, 0, {"state": "off", "running": 0}),
        (True, False, 2, {"state": "running", "running": 2}),
        (True, False, 0, {"state": "running", "running": 0}),
        (True, True, 1, {"state": "paused", "running": 1}),
    ],
)
def test_the_factory_state_is_public_and_small(settings, enabled, paused, running, expected):
    settings.runs_dir.mkdir(parents=True)
    assert factory(settings, enabled=enabled, paused=paused, running=running) == expected


# ---------- hidden dirs and files rewritten in place ----------

BASELINE_RUNS = (2500, 2600, 2700)


def hide(config, run_ids):
    """Hidden submission rows (like production's hidden house duplicates) for these run dirs."""
    Store(config.web_db)  # the schema
    rows = [
        (f"h{run_id}", f"hidden-{run_id}", "Buy KO.", "house", "scored", "2026-10-11T00:00:00",
         run_id, 1, "house")
        for run_id in run_ids
    ]  # fmt: skip
    with sqlite3.connect(config.web_db) as conn:
        conn.executemany(
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at, "
            "run_dir, hidden, lane) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.close()


def test_a_cold_board_never_parses_hidden_dirs(big, helpers, monkeypatch):
    hidden = [f"run-{i:04d}" for i in range(RUNS) if i not in BASELINE_RUNS][:2800]
    hide(big, hidden)
    calls = []
    real = bazaar_web.board.load_run
    monkeypatch.setattr(bazaar_web.board, "load_run", lambda path: calls.append(path) or real(path))
    with TestClient(create_app(big, helpers.FakeRunner())) as client:
        cold = client.get("/api/board").json()
        cold_parsed = len(calls)
        client.app.state.board.invalidate()  # a rebuild, as after the TTL
        client.get("/api/board")
        parsed = len(calls)
        with sqlite3.connect(big.web_db) as conn:  # un-hide the best run
            conn.execute("UPDATE submissions SET hidden = 0 WHERE run_dir = 'run-0000'")
        conn.close()
        client.app.state.board_cache.invalidate()  # raw SQL bypasses the store's change hook
        shown = client.get("/api/board").json()

    print(f"\n2,800 of 3,000 hidden: parsed {parsed}")
    assert parsed == cold_parsed  # the warm rebuild parsed nothing
    assert parsed == RUNS - 2800  # 197 agents and the 3 baselines
    assert not {p.name for p in calls[:parsed]} & set(hidden)
    assert cold["total"]["rows"] == 200
    assert shown["rows"][0]["name"] == "hidden-run-0000" and shown["rows"][0]["rank"] == 1
    assert [p.name for p in calls[parsed:]] == ["run-0000"]  # the next poll parsed it alone


def test_a_record_rewritten_in_place_is_parsed_again(tmp_path, helpers):
    runs = tmp_path / "runs"
    runs.mkdir()
    helpers.write_run(runs, "run-a", policy_ref="baseline-cash-only", period_return="0.01")
    source = BoardSource(runs, ttl=0)
    assert source.load()[0].ranked[0].period_return == helpers.Decimal("0.01")
    dir_mtime = (runs / "run-a").stat().st_mtime_ns

    # An operator re-seeds the baseline: the same dir, its files rewritten in place.
    helpers.write_run(tmp_path, "run-a", policy_ref="baseline-cash-only", period_return="0.03")
    for name in ("record.json", "evaluation.json"):
        target = runs / "run-a" / name
        before = target.stat().st_mtime_ns
        target.write_bytes((tmp_path / "run-a" / name).read_bytes())
        os.utime(target, ns=(before + 1_000_000, before + 1_000_000))  # coarse clocks too
    os.utime(runs / "run-a", ns=(dir_mtime, dir_mtime))  # the dir itself looks untouched

    assert source.load()[0].ranked[0].period_return == helpers.Decimal("0.03")
