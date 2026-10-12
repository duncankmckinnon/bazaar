import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from bazaar_web.app import STATIC, create_app
from fastapi.testclient import TestClient

BOARD = (STATIC / "board.html").read_text(encoding="utf-8")


def client_for(settings, helpers, **changes):
    return TestClient(create_app(dataclasses.replace(settings, **changes), helpers.FakeRunner()))


def test_board_page_injects_the_submit_url(settings, helpers):
    with client_for(settings, helpers, public_url="https://bazaar.example/") as client:
        page = client.get("/")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'const SUBMIT_URL = "https://bazaar.example/submit";' in page.text
    assert "__SUBMIT_URL__" not in page.text


def test_board_page_falls_back_to_the_request_url(settings, helpers):
    with client_for(settings, helpers) as client:
        page = client.get("/")

    assert 'const SUBMIT_URL = "http://testserver/submit";' in page.text


def test_board_page_loads_no_cdn_scripts():
    sources = re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", BOARD)

    assert sources == ["/static/vendor/qrcode.min.js", "/browser-telemetry.js?page=board"]
    for cdn in ("cdnjs", "unpkg", "jsdelivr"):
        assert cdn not in BOARD


def test_public_url_cannot_close_the_script_tag(settings, helpers):
    evil = 'https://x.example/</script><script>alert(1)</script>"'
    with client_for(settings, helpers, public_url=evil) as client:
        page = client.get("/").text

    assert "</script><script>alert(1)" not in page
    line = next(line for line in page.splitlines() if "const SUBMIT_URL" in line)
    assert line == (
        'const SUBMIT_URL = "https://x.example/<\\/script><script>alert(1)<\\/script>\\"/submit";'
    )


def test_vendored_qrcode_is_served_with_its_licence(settings, helpers):
    with client_for(settings, helpers) as client:
        script = client.get("/static/vendor/qrcode.min.js")

    assert script.status_code == 200
    assert "Copyright (c) 2009 Kazuhiko Arase" in script.text
    assert "var qrcode=function()" in script.text


def test_fonts_404_without_a_fonts_dir(settings, helpers):
    with client_for(settings, helpers) as client:
        assert client.get("/fonts/DwightMedium.woff2").status_code == 404


def test_fonts_are_served_from_the_fonts_dir(settings, helpers, tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "DwightMedium.woff2").write_bytes(b"wOF2-dummy")
    (tmp_path / "secret.woff2").write_bytes(b"outside")
    (fonts / "notes.txt").write_text("not a font")
    with client_for(settings, helpers, fonts_dir=fonts) as client:
        ok = client.get("/fonts/DwightMedium.woff2")
        missing = client.get("/fonts/Other.woff2")
        wrong_type = client.get("/fonts/notes.txt")

    assert ok.status_code == 200
    assert ok.content == b"wOF2-dummy"
    assert ok.headers["content-type"] == "font/woff2"
    assert missing.status_code == wrong_type.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/fonts/../secret.woff2",
        "/fonts/..%2fsecret.woff2",
        "/fonts/%2e%2e%2fsecret.woff2",
        "/fonts/..%5csecret.woff2",
        "/fonts/.woff2",
        "/static/../app.py",
        "/static/%2e%2e/app.py",
    ],
)
def test_traversal_is_refused(settings, helpers, tmp_path, path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (tmp_path / "secret.woff2").write_bytes(b"outside")
    with client_for(settings, helpers, fonts_dir=fonts) as client:
        response = client.get(path)

    assert response.status_code == 404
    assert b"outside" not in response.content
    assert b"create_app" not in response.content


def test_board_formats_percent_without_scaling():
    # The API already sends percent units; the page must not multiply by 100 again.
    assert (
        'const pct = (v) => (v > 0 ? "+" : v < 0 ? "-" : "") + Math.abs(v).toFixed(2) + "%";'
        in BOARD
    )
    script = BOARD.split("<script>", 1)[1]
    assert not re.search(r"\*\s*100\b|100\s*\*", script)
    # Score = starting cash x (1 + return_pct / 100), whole dollars, ASCII minus.
    assert "countTo(sc, start * (1 + r.return_pct / 100), money);" in script
    assert "Math.abs(Math.round(v))" in script
    assert "\u2212" not in script.replace("replace(/\\u2212/g", "")


def test_board_escapes_server_text():
    script = BOARD.split("<script>", 1)[1]
    assert "setName(nm, r.name, safeUrl(r.logfire_url));" in script
    # Server strings never reach an HTML string.
    for field in ("r.name", "r.handle", "r.status", "r.day", "r.logfire_url",
                  "s.name", "q.symbol", "day.date"):  # fmt: skip
        assert f"${{{field}}}" not in script
    assert 'innerHTML = \'<span class="rk">' in script  # static row skeleton only
    assert "day ${Number(r.day) || 0}/${DAYS}" in script  # numeric coercion, via textContent


def test_board_uses_the_v11_prototype_design():
    assert "<h1>Trading agent factory</h1>" in BOARD
    assert ".hd h1 { margin: 0; white-space: nowrap;" in BOARD  # one line at conference widths
    assert "<h2>Portfolio value by trading day</h2>" in BOARD
    assert '<div class="ar-coin">Insert policy</div>' in BOARD and "Scan to play" in BOARD
    assert (
        '<div class="ar-cols"><span>Rank</span><span>Score</span><span>Policy</span>'
        "<span>Return</span></div>"
    ) in BOARD
    assert (
        "background: radial-gradient(90% 70% at 40% 10%, #5a1d5e 0%, #2a1230 55%, #1c1220 100%);"
    ) in BOARD
    stylesheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', BOARD)
    assert len(stylesheets) == 1
    assert stylesheets[0].startswith("https://fonts.googleapis.com/css2?")
    assert "family=Silkscreen" in stylesheets[0] and "Press+Start" not in BOARD
    assert '<img class="logo" src="/static/brand/pydantic-name-dark.svg" alt="Pydantic"' in BOARD
    assert 'const COLORS = ["#d0ff71", "#00ffeb", "#e520e9"];' in BOARD  # 1st, 2nd, 3rd


def test_board_respects_reduced_motion():
    script = BOARD.split("<script>", 1)[1]
    assert "...(RM ? [] : quotes.map(quote))" in script  # one static copy of the tape
    assert "@media (prefers-reduced-motion: reduce)" in BOARD
    assert "animation: none;" in BOARD.split("@media (prefers-reduced-motion: reduce)", 1)[1]


def test_board_has_an_empty_state():
    assert "Scan to submit the first strategy" in BOARD


def test_static_files_exist():
    assert Path(STATIC / "vendor" / "qrcode.min.js").is_file()


def test_board_defines_the_helpers_its_functions_call():
    # A missing helper (esc, used by qrSVG) once stopped the whole script before any row
    # rendered; keep every shared helper defined.
    script = BOARD.split("<script>", 1)[1]
    for helper in ("esc", "pct", "money", "ordinal", "safeUrl"):
        assert f"const {helper} = " in script, helper
    for function in (
        "setName", "reconcile", "countTo", "qrSVG", "render", "poll", "svgEl", "renderChart",
        "renderBoard", "renderTape", "showTip", "applyFocus", "loadTickers", "tabFromHash",
        "onTab", "tabView", "factoryText", "renderTabs", "makeRow", "fillRow", "chartSeries",
        "fitPending", "qrFit", "placeQR",
    ):  # fmt: skip
        assert f"function {function}(" in script, function
    assert "esc(url)" in script  # qrSVG's fallback and aria-label


def test_chart_and_tape_build_data_with_dom_apis_not_html():
    script = BOARD.split("<script>", 1)[1]
    # SVG nodes come from createElementNS + setAttribute; labels and names via textContent.
    assert "document.createElementNS(SVG_NS, tag)" in script
    assert "el.setAttribute(k, String(v))" in script
    assert "b.textContent = best.s.name;" in script  # tooltip title
    assert "b.textContent = String(q.symbol);" in script  # tape
    assert "empty.textContent = EMPTY[tab];" in script  # the empty state
    # The only innerHTML writes are the static row skeleton, the QR and clearing the list.
    writes = re.findall(r"innerHTML = ([^;]+);", script)
    skeleton = (
        '\'<span class="rk"></span><span class="sc num"></span><span class="nm"></span>'
        '<span class="rt num"></span>\''
    )
    assert sorted(writes) == sorted(['qrSVG(SUBMIT_URL, "#36182D", "#E320E7")', '""', skeleton])


def test_chart_draws_the_start_reference_and_replay_label():
    assert 'rt.textContent = money(start) + " start";' in BOARD
    assert "stroke-dasharray: 6 6" in BOARD  # dashed $10,000 reference
    assert "Feb 2" not in BOARD.replace('"Feb 2–13 2026" within a month', "")  # derived
    assert 'fetch("/api/tickers"' in BOARD


def run_js(function, call):
    """Run one board.html function under node; there is no JS toolchain, only the function."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    pattern = rf"^function {function}\(.*?^}}$"
    source = re.search(pattern, BOARD, re.DOTALL | re.MULTILINE).group(0)
    script = f"{source}\nconsole.log(JSON.stringify({call}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize(
    ("start", "end", "label"),
    [
        ("2026-02-02", "2026-02-13", "Feb 2–13 2026"),
        ("2026-09-28", "2026-10-09", "Sep 28 – Oct 9 2026"),
        ("2025-12-29", "2026-01-09", "Dec 29 2025 – Jan 9 2026"),
        (None, None, None),
        ("2026-02-02", None, None),
        ("2026-13-01", "2026-13-05", None),
        ("<img src=x>", "2026-02-13", None),
    ],
)
def test_the_window_label_is_derived_from_the_dates(start, end, label):
    assert run_js("windowLabel", f"windowLabel({json.dumps(start)}, {json.dumps(end)})") == label


def test_the_header_and_tape_take_the_window_from_the_data():
    script = BOARD.split("<script>", 1)[1]
    assert "const label = windowLabel(w.start, w.end);" in script  # header, from /api/board
    assert "tapeWindow = data.window ? windowLabel(data.window.start, data.window.end)" in script
    assert "if (!tapeWindow) tapeDays = [];" in script  # no window, no tape
    assert '$("sub-a").textContent' in script and '$("sub-b").textContent' in script
    assert '$("sub-c").textContent = factoryText(data.factory);' in script


def test_the_conference_layout_puts_the_qr_strip_under_the_chart():
    assert '"hd hd" "board chart" "board side" "tape tape"' in BOARD
    # The prototype's 900 px table column and 330 px QR strip; the chart takes the rest.
    assert (
        "grid-template-columns: calc(var(--px)*900) minmax(0,1fr); grid-template-rows: auto "
        "minmax(0,1fr) calc(var(--px)*330) auto;"
    ) in BOARD
    body = BOARD.split("<main", 1)[1]
    assert body.index('class="chart"') < body.index('class="side"')


def test_a_tall_window_moves_the_qr_block_under_the_table():
    # qr-left: the chart takes the whole right column and the QR block sits at the bottom of the
    # left column, above the footnote.
    assert (
        '.dash.qr-left { grid-template-rows: auto minmax(0,1fr) auto; grid-template-areas: "hd hd" '
        '"board chart" "tape tape"; }'
    ) in BOARD
    assert ".board .ar-qr { width: var(--qr); box-sizing: border-box; }" in BOARD
    script = BOARD.split("<script>", 1)[1]
    assert 'if (left) board.insertBefore(side, note); else dash.insertBefore(side, $("tape"));' in (
        script
    )
    assert "new ResizeObserver(placeQR).observe(board);" in script


def test_the_qr_block_goes_left_only_when_a_large_qr_and_3_pending_rows_fit():
    def fit(px, board, above, note, width, cta):
        m = {"px": px, "board": board, "above": above, "note": note, "width": width, "cta": cta}
        return run_tabs(f"qrFit({json.dumps(m)})")

    # free = board - above - 15 rows (40) - 3 pending rows and a "+K more" line (34)
    #        - note - two 12 px gaps - the frame's 12 px above and below
    assert fit(1, 945, 120, 20, 900, 500) == 0  # 1920x1080: 21 px free, the strip stays
    assert fit(1, 1100, 120, 20, 900, 300) == 0  # 176 px free: smaller than the strip's 300
    assert fit(1, 1224, 120, 20, 900, 300) == 300  # exactly the strip's size
    assert fit(1, 1500, 120, 20, 900, 300) == 420  # capped
    assert fit(0.5, 900, 60, 33, 450, 160) == 260  # 960 wide: the column width limits it


def test_a_running_rows_day_text_widens_its_column_instead_of_clipping():
    # The blinking dot (20886e3) made "day 4/10" overflow the fixed 110 px return column.
    assert (
        ".pending .ar-row { grid-template-columns: calc(var(--px)*80) calc(var(--px)*140) "
        "minmax(0,1fr) minmax(calc(var(--px)*110), max-content); }"
    ) in BOARD
    phone = BOARD.split("@media (max-width: 760px) {", 1)[1].split("\n}", 1)[0]
    assert (
        ".pending .ar-row { grid-template-columns: 42px 80px minmax(0,1fr) "
        "minmax(70px, max-content); }"
    ) in phone


def test_pending_rows_that_do_not_fit_become_a_more_line():
    assert '<p class="pending-more" id="pending-more" hidden></p>' in BOARD
    # .ar-row sets display: grid, which would override the hidden attribute.
    assert ".pending .ar-row[hidden] { display: none; }" in BOARD
    script = BOARD.split("<script>", 1)[1]
    assert "more.textContent = `+${rows.length - shown} more`;" in script
    assert "new ResizeObserver(fitPending).observe(pendingList);" in script


def test_the_board_has_no_market_wire():
    # L3: the market wire panel is gone, markup, style and script. /api/board still sends
    # "events"; the board just doesn't render them.
    assert "wire" not in BOARD.lower()
    script = BOARD.split("<script>", 1)[1]
    assert "data.events" not in script and "ev.text" not in script


def test_the_qr_keeps_a_four_module_quiet_zone_and_error_correction_h():
    script = BOARD.split("<script>", 1)[1]
    assert 'qrcode(0, "H")' in script
    assert re.search(r"\bpad = 4, size = n \+ pad \* 2;", script)


def test_the_chart_has_no_legend():
    assert "legend" not in BOARD.lower()


def test_the_running_day_text_is_steady_and_a_marker_blinks():
    # 'day d/10' sits in .rt; a blinking .rt hid it half of each second (and from photos).
    rules = dict(re.findall(r"^\s*([^{}@/][^{}]*?)\s*\{([^}]*)\}", BOARD, re.MULTILINE))
    data_text = [sel for sel in rules if sel.endswith((".rt", ".ar-coin"))]
    assert data_text, "the day text's rules"
    for selector in data_text:
        assert "animation" not in rules[selector], selector
    marker = rules[".pending .ar-row.st-running .rt::before"]
    assert "animation: blink 1s steps(1) infinite" in marker and "var(--aqua)" in marker
    reduced = BOARD.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".pending .ar-row.st-running .rt::before" in reduced.split("}", 1)[0]


def test_the_hover_cursor_is_hidden_until_hovered():
    script = BOARD.split("<script>", 1)[1]
    assert 'id: "cursor", visibility: "hidden" }, svg);' in script  # parked at x=-10, unseen
    assert script.count('cursor.setAttribute("visibility", "visible");') == 1  # on hover only
    assert 'c.setAttribute("visibility", "hidden"); }' in script  # and hidden on leave


def run_tabs(expression):
    """Evaluate an expression in node with the page's tab and chart helpers loaded."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    sources = re.findall(r"^const (?:PALETTE|BASELINE_COLORS) = .*$", BOARD, re.MULTILINE)
    for marker in ("tabs", "chartSeries", "qrFit"):
        sources += re.findall(rf"// {marker}:start(.*?)// {marker}:end", BOARD, re.DOTALL)
    script = "\n".join(sources) + f"\nconsole.log(JSON.stringify({expression}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_the_tab_comes_from_the_url_hash():
    hashes = ["#human", "#factory", "#both", "", "#", "#HUMAN", "#other", "#__proto__", "human"]

    assert run_tabs(f"{json.dumps(hashes)}.map(tabFromHash)") == [
        "human", "factory", "both", "both", "both", "both", "both", "both", "human"
    ]  # fmt: skip
    script = BOARD.split("<script>", 1)[1]
    assert 'addEventListener("hashchange", () => {' in script
    assert "location.hash = b.dataset.tab;" in script  # a click sets the hash


def test_tabs_are_real_tab_buttons_44px_tall():
    tabs = re.findall(r'<button type="button" class="tab" role="tab"[^>]*>([^<]*)</button>', BOARD)

    assert tabs == ["Human", "Agent factory", "Both"]
    assert 'role="tablist"' in BOARD and 'role="tabpanel"' in BOARD
    assert 'b.setAttribute("aria-selected", String(on));' in BOARD
    assert re.search(r"\.tab \{ min-height: max\(44px,", BOARD)
    assert re.search(r'\.tab\[aria-selected="true"\] \{ background: var\(--lime\);', BOARD)


def tab_rows():
    return [
        *({"id": f"f{i}", "lane": "house", "kind": "agent", "status": "scored", "rank": i,
           "lane_rank": i} for i in range(1, 21)),
        {"id": "base", "lane": None, "kind": "baseline", "status": "scored", "rank": 21,
         "lane_rank": None},
        *({"id": f"h{i}", "lane": "human", "kind": "agent", "status": "scored", "rank": 21 + i,
           "lane_rank": i} for i in range(1, 18)),
        {"id": "run-h", "lane": "human", "kind": "agent", "status": "running", "rank": None},
        {"id": "run-f", "lane": "house", "kind": "agent", "status": "running", "rank": None},
        {"id": "q-h", "lane": "human", "kind": "agent", "status": "queued", "rank": None},
    ]  # fmt: skip


def view_of(rows, tab):
    return run_tabs(
        f"(function () {{ var v = tabView({json.dumps(rows)}, {json.dumps(tab)});"
        " return [v.scored.map(function (r) { return [r.id, r.place]; }),"
        " v.pending.map(function (r) { return r.id; })]; })()"
    )


def test_each_tab_shows_its_top_15_by_its_own_rank_then_its_pending_rows():
    rows = tab_rows()

    assert view_of(rows, "human") == [[[f"h{i}", i] for i in range(1, 16)], ["run-h", "q-h"]]
    assert view_of(rows, "factory") == [[[f"f{i}", i] for i in range(1, 16)], ["run-f"]]
    assert view_of(rows, "both") == [
        [[f"f{i}", i] for i in range(1, 16)],
        ["run-h", "run-f", "q-h"],
    ]


def test_baselines_are_listed_on_both_only():
    rows = tab_rows()
    rows[20]["rank"] = 2  # a strong baseline, 2nd overall

    assert ["base", 2] in view_of(rows, "both")[0]
    for tab in ("human", "factory"):
        assert "base" not in {row_id for row_id, _ in view_of(rows, tab)[0]}


def test_each_tab_charts_its_own_lines_and_every_baseline():
    path = [{"day": d, "value": 10000 + d} for d in range(1, 11)]
    rows = [
        {"id": "f1", "name": "fac", "lane": "house", "kind": "agent", "status": "scored",
         "chart_tabs": {"both": 3, "factory": 2}, "history": path},
        {"id": "h1", "name": "hum", "lane": "human", "kind": "agent", "status": "scored",
         "chart_tabs": {"human": 1}, "history": path},
        {"id": "b", "name": "SPY", "lane": None, "kind": "baseline", "status": "scored",
         "chart_tabs": None, "history": path},
        {"id": "r", "name": "live", "lane": "human", "kind": "agent", "status": "running",
         "chart_tabs": None, "history": path},
    ]  # fmt: skip
    series = run_tabs(
        f"(function () {{ var rows = {json.dumps(rows)}; var out = {{}};"
        " ['human', 'factory', 'both'].forEach(function (t) { out[t] = chartSeries(rows, t)"
        ".map(function (s) { return [s.name, s.baseline]; }); }); return out; })()"
    )

    assert series["human"] == [["hum", False], ["SPY", True], ["live", False]]
    assert series["factory"] == [["fac +1", False], ["SPY", True]]
    assert series["both"] == [["fac +2", False], ["SPY", True], ["live", False]]


def test_the_header_states_the_factory():
    calls = [
        {"state": "running", "running": 1}, {"state": "running", "running": 2},
        {"state": "running", "running": 0}, {"state": "paused", "running": 1},
        {"state": "off", "running": 0}, None, {"state": "exploded"},
        {"state": "running", "running": "3"},
    ]  # fmt: skip

    assert run_tabs(f"{json.dumps(calls)}.map(factoryText)") == [
        "Factory: 1 agent running", "Factory: 2 agents running", "Factory: 0 agents running",
        "Factory: paused", "Factory: off", "", "", "Factory: 0 agents running",
    ]  # fmt: skip
    assert '<span id="sub-c" class="factory"></span>' in BOARD
    assert ".hd .sub .factory { color: var(--lime); }" in BOARD


def test_house_rows_get_a_dim_ai_tag_by_text_only():
    script = BOARD.split("<script>", 1)[1]
    assert 'if (r.lane === "house" || r.handle === "house") {' in script
    house = script.split('if (r.lane === "house" || r.handle === "house") {', 1)[1]
    house = house.split("\n  }\n", 1)[0]  # the block's closing brace, in fillRow
    # Every house row gets the AI chip, unconditionally; the G chip only for a generation > 0.
    assert 'const chips = ["AI"];' in house
    assert "const gen = Number(r.generation);" in house
    assert "if (Number.isInteger(gen) && gen > 0) chips.push(`G${gen}`);" in house
    assert 'tag.className = "ai";' in house and "tag.textContent = text;" in house
    assert "nm.prepend(...chips.map(" in house  # AI first, then G<n>
    assert "innerHTML" not in house
    # All chips removed before setName runs, so re-renders don't stack them and the name
    # link is found and kept as before.
    assert script.index('nm.querySelectorAll(".ai").forEach((chip) => chip.remove());') < (
        script.index("setName(nm, r.name, safeUrl(r.logfire_url));")
    )
    assert ".ar-row .nm .ai {" in BOARD and "color: var(--muted)" in BOARD


def test_the_board_page_has_the_in_sample_footnote(settings, helpers):
    from bazaar_web.app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        page = client.get("/").text

    assert (
        '<p class="ar-note"><span class="ai">AI</span> = house strategy \u00b7 '
        '<span class="ai">Gn</span> = bred from this window\'s results (in-sample)</p>'
    ) in page
    # Static markup under the table, never built by the script.
    assert "ar-note" not in page.split("<script>", 1)[1]
    assert page.index('id="list"') < page.index('class="ar-note"')


def test_the_header_names_the_universe_from_the_board_data():
    script = BOARD.split("<script>", 1)[1]
    assert 'const universe = typeof w.universe === "string" ? w.universe : "";' in script
    assert "S&P" not in BOARD  # never hardcoded
    assert "STOCKS" not in script  # the count label comes from the server too
