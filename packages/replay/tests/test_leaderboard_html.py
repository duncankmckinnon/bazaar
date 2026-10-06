import os
import subprocess
import sys
from pathlib import Path

from bazaar_replay.leaderboard import load_board
from bazaar_replay.leaderboard_html import render

GOLDEN = Path(__file__).parent / "golden" / "leaderboard.html"


def test_cli_writes_the_page(tmp_path, demo_runs):
    runs = tmp_path / "runs"
    runs.mkdir()
    demo_runs(runs)
    out = tmp_path / "board.html"

    subprocess.run(
        [sys.executable, "-m", "bazaar_replay.leaderboard", str(runs), "-o", str(out)], check=True
    )

    assert out.read_text(encoding="utf-8") == render(load_board(runs))


def test_page_matches_golden_snapshot(tmp_path, demo_runs):
    demo_runs(tmp_path)
    html = render(load_board(tmp_path))

    # Regenerate on purpose only: BAZAAR_UPDATE_GOLDEN=1 uv run pytest packages/replay
    if os.environ.get("BAZAAR_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(html, encoding="utf-8")
    assert html == GOLDEN.read_text(encoding="utf-8")


def test_page_has_no_script_or_external_urls(tmp_path, demo_runs):
    demo_runs(tmp_path)
    html = render(load_board(tmp_path)).lower()

    assert "<script" not in html
    assert "http" not in html


def test_untrusted_text_is_escaped(tmp_path, write_run):
    write_run(tmp_path, "bh", policy_ref="baseline-buy-and-hold", period_return="0.01")
    write_run(tmp_path, "evil", policy_ref="<b>pwn</b>", period_return="0.02")
    write_run(
        tmp_path,
        "crash",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        status="failed",
        failure="<img src=x>",
    )
    html = render(load_board(tmp_path))

    assert "<b>pwn</b>" not in html
    assert "&lt;b&gt;pwn&lt;/b&gt;" in html
    assert "<img" not in html


def test_markers_and_labels(tmp_path, demo_runs):
    demo_runs(tmp_path)
    html = render(load_board(tmp_path))

    assert html.count("⚠ not reconciled") == 1
    assert html.count('<span class="label">computed</span>') == 2
    assert "synthetic prices" in html
    assert "+2.10%" in html
    assert "-3.10%" in html
    assert "<code>0af7651916cd43dd8448eb211c80319c</code>" in html


def test_real_data_version_is_not_labelled_synthetic(tmp_path, write_run):
    write_run(tmp_path, "bh", policy_ref="baseline-buy-and-hold", period_return="0.01")
    board = load_board(tmp_path)
    real = board.model_copy(
        update={"header": board.header.model_copy(update={"data_version": "alpaca-2026-02"})}
    )

    assert "synthetic prices" not in render(real)


def test_refused_run_is_listed_with_its_code(tmp_path, demo_runs):
    demo_runs(tmp_path)
    html = render(load_board(tmp_path))

    assert "<h2>Refused / failed</h2>" in html
    assert "Failed runs" not in html
    assert "approval_denied: refused before any account was opened" in html


def test_failure_code_is_escaped(tmp_path, write_run):
    write_run(
        tmp_path,
        "r",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        status="failed",
        failure="x",
        failure_code="<i>denied</i>",
        account=False,
        evaluation=False,
    )

    assert "&lt;i&gt;denied&lt;/i&gt;: x" in render(load_board(tmp_path))
