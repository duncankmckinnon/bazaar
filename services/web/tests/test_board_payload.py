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
        _BUILT["runs"] = runs
    return _BUILT["runs"]


@pytest.fixture
def big(settings, big_runs, tmp_path):
    runs = tmp_path / "runs"
    shutil.copytree(big_runs, runs)
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


def test_the_payload_is_small_and_a_warm_request_fast(big, helpers):
    runner = helpers.FakeRunner()
    runner.gate.clear()
    with TestClient(create_app(big, runner)) as client:
        sid = client.post(
            "/api/submissions",
            json={"name": "live-bot", "handle": None, "instructions": "Buy KO on dips, hold MSFT.",
                  "email": "a@example.com", "company": "Co", "role": "Dev"},
        ).json()["id"]  # fmt: skip
        helpers.wait_for(lambda: client.get(f"/api/submissions/{sid}").json()["day"] == 10)
        start = time.perf_counter()
        cold = client.get("/api/board")
        cold_s = time.perf_counter() - start
        start = time.perf_counter()
        warm = client.get("/api/board")
        warm_s = time.perf_counter() - start
        runner.gate.set()

    body = warm.json()
    print(f"\n3,000 runs: {len(cold.content)} bytes, first {cold_s:.3f} s, warm {warm_s:.3f} s")
    assert len(warm.content) < 100_000
    assert warm_s < 0.5
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

    charted = {
        r["id"] for r in rows
        if r["kind"] == "baseline" or r["status"] == "running"
        or (r["status"] == "scored" and r["rank"] <= 10)
    }  # fmt: skip
    assert {r["id"] for r in rows if r["history"] is not None} == charted
    assert len(charted) == 10 + len(BASELINES)  # the top 10 and the baselines; none running
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
    assert sid not in {r["id"] for r in board}  # ranked > 100: not sent, still ranked


def test_each_run_dir_is_parsed_once(big, helpers, monkeypatch):
    calls = []
    real = bazaar_web.board.load_run
    monkeypatch.setattr(bazaar_web.board, "load_run", lambda path: calls.append(path) or real(path))
    source = BoardSource(big.runs_dir, ttl=0)

    first, _ = source.load()
    assert len(calls) == RUNS
    start = time.perf_counter()
    again, _ = source.load()
    warm_s = time.perf_counter() - start
    assert len(calls) == RUNS and again.ranked == first.ranked  # nothing re-parsed
    print(f"\nwarm rebuild of 3,000 runs: {warm_s:.3f} s")
    assert warm_s < 0.5

    shutil.rmtree(big.runs_dir / "run-0000")  # the best run goes away
    helpers.write_run(big.runs_dir, "run-new", policy_ref="agent-new", period_return="0.5")
    board, histories = source.load()
    assert [p.name for p in calls[RUNS:]] == ["run-new"]  # only the new dir parsed
    assert board.ranked[0].run_id == "run-new" and "run-0000" not in histories
    assert len(board.ranked) == RUNS


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
    assert set(body) == {"window", "rows", "total", "events"}


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
        start = time.perf_counter()
        cold = client.get("/api/board").json()
        cold_s = time.perf_counter() - start
        start = time.perf_counter()
        client.get("/api/board")
        warm_s = time.perf_counter() - start
        parsed = len(calls)
        with sqlite3.connect(big.web_db) as conn:  # un-hide the best run
            conn.execute("UPDATE submissions SET hidden = 0 WHERE run_dir = 'run-0000'")
        conn.close()
        shown = client.get("/api/board").json()

    print(f"\n2,800 of 3,000 hidden: parsed {parsed}, cold {cold_s:.3f} s, warm {warm_s:.3f} s")
    assert parsed == RUNS - 2800  # 197 agents and the 3 baselines
    assert not {p.name for p in calls[:parsed]} & set(hidden)
    assert cold["total"]["rows"] == 200 and warm_s < 0.5
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
