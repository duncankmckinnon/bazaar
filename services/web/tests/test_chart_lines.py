"""L2: the chart draws 10 visually distinct lines; identical paths share one line, "+N"."""

import itertools
import math
import re

from bazaar_web.app import STATIC
from bazaar_web.board import CHART_LINES, page_rows

START = 10_000.0


def path(delta):
    """Ten days of values: START + delta * day."""
    return [{"day": d, "value": round(START + delta * d, 2)} for d in range(1, 11)]


def scored(rank, history, *, kind="agent"):
    return {"id": f"r{rank}", "name": f"bot-{rank}", "kind": kind, "status": "scored",
            "rank": rank, "history": history}  # fmt: skip


def lines(rows):
    shown = page_rows(rows)
    return [(r["name"], r["chart_members"]) for r in shown if r["chart_members"] is not None]


def test_identical_and_near_identical_paths_share_a_line_and_ten_lines_are_drawn():
    top = path(94.0)
    near = [dict(p, value=round(p["value"] + 0.5 * (p["day"] % 2), 2)) for p in top]  # +$0.50
    rows = [scored(r, top if r % 2 else near) for r in range(1, 10)]  # 9 on one path
    rows += [scored(r, path(80.0 - r)) for r in range(10, 20)]  # 10 distinct below

    drawn = lines(rows)

    assert len(drawn) == CHART_LINES == 10
    assert drawn[0] == ("bot-1", 9)  # labelled "bot-1 +8" by the page
    assert [name for name, _ in drawn[1:]] == [f"bot-{r}" for r in range(10, 19)]  # rank order
    assert all(n == 1 for _, n in drawn[1:])


def test_one_day_more_than_a_dollar_apart_is_a_new_line():
    base = path(50.0)
    dollar = [dict(p, value=p["value"] + 1.0) if p["day"] == 7 else p for p in base]
    over = [dict(p, value=round(p["value"] + 1.01, 2)) if p["day"] == 7 else p for p in base]
    rows = [scored(1, base), scored(2, dollar), scored(3, over)]

    assert lines(rows) == [("bot-1", 2), ("bot-3", 1)]


def test_fewer_distinct_paths_give_that_many_lines():
    rows = [scored(r, path(10.0 * (r % 3))) for r in range(1, 13)]  # three distinct paths
    assert [n for _, n in lines(rows)] == [4, 4, 4]


def test_members_keep_their_rows_without_history():
    rows = [scored(r, path(5.0)) for r in range(1, 5)]
    shown = page_rows(rows)
    assert [r["id"] for r in shown] == ["r1", "r2", "r3", "r4"]
    assert shown[0]["history"] is not None and shown[0]["chart_members"] == 4
    assert all(r["history"] is None and r["chart_members"] is None for r in shown[1:])


def test_baselines_always_have_history_and_are_never_grouped():
    same = path(20.0)
    rows = [scored(1, same), scored(2, same, kind="baseline"), scored(3, same)]
    shown = {r["id"]: r for r in page_rows(rows)}

    assert shown["r2"]["history"] == same and shown["r2"]["chart_members"] is None
    assert shown["r1"]["chart_members"] == 2 and shown["r3"]["history"] is None


def test_rows_without_history_are_never_a_line():
    rows = [scored(1, None), scored(2, path(3.0)), scored(3, [])]
    assert lines(rows) == [("bot-2", 1)]


def test_the_last_line_counts_its_identical_neighbours():
    rows = [scored(r, path(float(r))) for r in range(1, 10)]  # 9 distinct lines
    rows += [scored(10, path(50.0)), scored(11, path(50.0)), scored(12, path(60.0))]
    assert lines(rows)[-1] == ("bot-10", 2)


# ---------- the page ----------

BOARD = (STATIC / "board.html").read_text()


def lab(hex_colour):
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def linear(c):
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = linear(r), linear(g), linear(b)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(v):
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def test_the_top_3_lines_are_the_podium_colours_and_the_rest_one_plum_grey():
    # V1: lime, cyan and magenta only for ranks 1-3; every other line one muted plum-grey.
    (raw,) = re.findall(r"const COLORS = \[([^\]]*)\];", BOARD)
    podium = re.findall(r'"(#[0-9a-fA-F]{6})"', raw)
    assert podium == ["#d0ff71", "#00ffeb", "#e520e9"]
    (dim,) = re.findall(r'const DIM = "(#[0-9a-fA-F]{6})";', BOARD)
    assert dim == "#8d7a96"
    closest = min(math.dist(lab(a), lab(b)) for a, b in itertools.combinations([*podium, dim], 2))
    assert closest >= 30, closest
    script = BOARD.split("<script>", 1)[1]
    assert (
        "const color = baseline ? BASELINE_COLORS[base++ % BASELINE_COLORS.length] : podium ? COLORS[place - 1] : DIM;"
        in script
    )
    assert "const place = baseline || running ? null : ++rank;" in script  # running lines are grey
    assert ".plot .series.dim polyline { stroke-width: 2; opacity: .6; }" in BOARD


def test_the_page_draws_representatives_labelled_with_their_group():
    script = BOARD.split("<script>", 1)[1]
    assert (
        'rows.filter(r => r.status === "scored" && r.kind !== "baseline" && '
        "Number.isInteger(members(r))).forEach(add);"
    ) in script
    assert 'const more = Number.isInteger(n) && n > 1 ? ` +${n - 1}` : "";' in script
    assert ".slice(0, 10)" not in script  # no fixed top 10 any more
    assert "b.textContent = best.s.name;" in script  # the line's name, as text only
