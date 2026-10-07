"""Render a Leaderboard as one self-contained HTML page: inline CSS, no script, no links."""

import argparse
from decimal import Decimal
from html import escape
from pathlib import Path

from bazaar_replay.leaderboard import Entry, Leaderboard, load_board

FOOTER = "Scored after each run with full-timeline evidence. Agents never see these scores."
DASH = "—"

CSS = """
:root { color-scheme: light; }
body { margin: 0; padding: 18px 32px; background: #fbfbf8; color: #111;
  font: 20px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { margin: 0; font-size: 34px; }
.meta { margin: 2px 0 12px; color: #333; }
.tag { display: inline-block; padding: 1px 10px; border: 2px solid #8a5a00; border-radius: 6px;
  color: #8a5a00; font-weight: 600; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 14px; text-transform: uppercase; letter-spacing: .03em;
  color: #333; border-bottom: 3px solid #111; padding: 4px 8px; vertical-align: bottom; }
td { padding: 6px 8px; border-bottom: 1px solid #ccc; vertical-align: top; }
td.name { white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.reference td { background: #eef3fb; }
.sub { display: block; font-size: 14px; color: #555; }
.pos { color: #0a6b2d; font-weight: 600; }
.neg { color: #b00020; font-weight: 600; }
.bad { color: #b00020; font-weight: 700; }
.warn { display: block; color: #8a5a00; font-weight: 700; font-size: 16px; }
.label { font-size: 13px; color: #555; border: 1px solid #999; border-radius: 4px; padding: 0 4px;
  margin-left: 4px; font-weight: 400; }
code { font-size: 13px; user-select: all; -webkit-user-select: all; background: #eee;
  padding: 2px 4px; border-radius: 4px; }
.sides { display: grid; grid-template-columns: 1fr 1fr; gap: 0 32px; }
section { margin-top: 10px; font-size: 16px; }
h2 { font-size: 16px; margin: 0 0 2px; }
ul { margin: 0; padding-left: 22px; }
footer { margin-top: 12px; font-size: 15px; color: #333; }
"""


def percent(value: Decimal | None) -> str:
    if value is None:
        return DASH
    css = "pos" if value > 0 else "neg" if value < 0 else ""
    text = f"{value * 100:+.2f}%"
    return f'<span class="{css}">{text}</span>' if css else text


def strategy(entry: Entry) -> str:
    name = escape(entry.policy_ref or entry.run_id)
    version = f" · {str(entry.strategy_version_id)[:8]}" if entry.strategy_version_id else ""
    reference = " · buy-and-hold reference" if entry.is_reference else ""
    cell = f'{name}<span class="sub">{escape(version + reference).lstrip(" ·")}</span>'
    if entry.not_reconciled:
        cell += '<span class="warn">⚠ not reconciled</span>'
    if entry.decision_error_count:
        cell += f'<span class="warn">⚠ decision errors: {entry.decision_error_count}</span>'
    return cell


def count(n: int, *, bad: bool = False) -> str:
    return f'<span class="bad">{n}</span>' if bad and n else str(n)


def scores(entry: Entry) -> str:
    s = entry.trade_scores
    cell = " / ".join(
        (
            count(s.get("scored", 0)),
            count(s.get("failed", 0), bad=True),
            count(s.get("unsupported", 0), bad=True),
        )
    )
    if pending := s.get("pending", 0):
        cell += f'<span class="sub">{pending} pending</span>'
    return cell


def row(rank: int, entry: Entry) -> str:
    excess = percent(entry.excess_vs_buy_and_hold)
    if entry.excess_computed:
        excess += '<span class="label">computed</span>'
    trace = f"<code>{escape(entry.trace_id)}</code>" if entry.trace_id else DASH
    cells = (
        f'<td class="num">{rank}</td>',
        f'<td class="name">{strategy(entry)}</td>',
        f"<td>{escape(entry.kind or DASH)}</td>",
        f'<td class="num">{percent(entry.period_return)}</td>',
        f'<td class="num">{excess}</td>',
        f'<td class="num">{entry.orders_filled} / {count(entry.orders_rejected)}</td>',
        f'<td class="num">{scores(entry)}</td>',
        f"<td>{trace}</td>",
    )
    css = ' class="reference"' if entry.is_reference else ""
    return f"<tr{css}>{''.join(cells)}</tr>"


def failure(entry: Entry) -> str:
    reason = entry.reason or ""
    return f"{entry.failure_code}: {reason}" if entry.failure_code else reason


def decision_errors(rank: int, entry: Entry) -> str:
    n = entry.decision_error_count
    errors = f"{n} decision error{'s' if n != 1 else ''}"
    at = f"{entry.first_decision_error_at:%Y-%m-%d %H:%M} UTC"
    first = escape(entry.first_decision_error or "")
    return f"#{rank} {escape(entry.policy_ref or entry.run_id)}: {errors}; first at {at}: {first}"


def side_list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{item}</li>" for item in items)
    return f"<section><h2>{title}</h2><ul>{lis}</ul></section>"


def header_line(board: Leaderboard) -> str:
    h = board.header
    if h is None:
        return '<p class="meta">No comparable runs yet.</p>'
    parts = [
        f"{h.start_at:%Y-%m-%d} → {h.end_at:%Y-%m-%d}",
        f"starting cash ${h.starting_cash:,.2f}",
        f"data {escape(h.data_version)}",
        f"schedule {escape(h.schedule_digest)}",
    ]
    line = " · ".join(parts)
    if any(word in h.data_version.lower() for word in ("synthetic", "fixture")):
        line += ' <span class="tag">synthetic prices</span>'
    return f'<p class="meta">{line}</p>'


def render(board: Leaderboard) -> str:
    head = (
        "<tr><th>#</th><th>Strategy</th><th>Kind</th><th>Return</th>"
        "<th>Excess vs<br>buy-and-hold</th><th>Trades<br>filled / rej.</th>"
        "<th>Scores<br>scored / failed / unsup.</th><th>Trace</th></tr>"
    )
    body = "".join(row(rank, entry) for rank, entry in enumerate(board.ranked, start=1))
    sides = (
        side_list(
            "Decision errors",
            [
                decision_errors(rank, e)
                for rank, e in enumerate(board.ranked, start=1)
                if e.decision_error_count
            ],
        )
        + side_list(
            "Refused / failed",
            [f"{escape(e.policy_ref or e.run_id)}: {escape(failure(e))}" for e in board.failed],
        )
        + side_list(
            "Not comparable",
            [
                f"{escape(e.policy_ref or e.run_id)}: {escape(e.mismatch or '')}"
                for e in board.not_comparable
            ],
        )
        + side_list(
            "Invalid",
            [f"{escape(e.run_id)}: {escape(e.reason or '')}" for e in board.invalid],
        )
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        "<title>Agent Leaderboard</title>"
        f"<style>{CSS}</style></head><body>"
        f"<h1>Agent Leaderboard</h1>{header_line(board)}"
        f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"
        f'<div class="sides">{sides}</div><footer>{FOOTER}</footer></body></html>\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bazaar_replay.leaderboard",
        description="Write the leaderboard HTML for a directory of run results.",
    )
    parser.add_argument("runs_dir", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.write_text(render(load_board(args.runs_dir)), encoding="utf-8")
    return 0
