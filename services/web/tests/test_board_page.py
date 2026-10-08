import dataclasses
import re
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

    assert sources == ["/static/vendor/qrcode.min.js"]
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
    assert 'setName(li.querySelector(".nm"), r.name, safeUrl(r.logfire_url));' in script
    assert "text.textContent = String(ev.text);" in script  # market wire
    # Server strings never reach an HTML string.
    for field in ("r.name", "r.handle", "r.status", "r.day", "ev.text", "r.logfire_url",
                  "s.name", "q.symbol", "day.date"):  # fmt: skip
        assert f"${{{field}}}" not in script
    assert 'innerHTML = \'<span class="rk">' in script  # static row skeleton only
    assert "DAY ${Number(r.day) || 0}/${DAYS}" in script  # numeric coercion, via textContent


def test_board_uses_the_arcade_design_on_the_podium_background():
    assert "HIGH SCORES" in BOARD
    assert "TRADING AGENT FACTORY DASHBOARD" in BOARD
    assert "PORTFOLIO VALUE BY TRADING DAY" in BOARD
    assert "MARKET WIRE" in BOARD and "INSERT POLICY" in BOARD
    assert (
        "background: radial-gradient(60% 70% at 50% 18%, rgba(229,32,233,.30) 0%, "
        "rgba(229,32,233,0) 60%), radial-gradient(50% 60% at 90% 100%, "
        "rgba(255,101,80,.16) 0%, rgba(255,101,80,0) 60%), #36182D;"
    ) in BOARD
    stylesheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', BOARD)
    assert len(stylesheets) == 1
    assert stylesheets[0].startswith("https://fonts.googleapis.com/css2?")
    assert "family=Press+Start+2P" in stylesheets[0]
    assert "const MAX_ROWS = 10;" in BOARD
    assert 'all.filter(r => r.status !== "failed").slice(0, MAX_ROWS)' in BOARD


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
        "renderBoard", "renderWire", "renderTape", "showTip", "applyFocus", "loadTickers",
    ):  # fmt: skip
        assert f"function {function}(" in script, function
    assert "esc(url)" in script  # qrSVG's fallback and aria-label


def test_chart_and_tape_build_data_with_dom_apis_not_html():
    script = BOARD.split("<script>", 1)[1]
    # SVG nodes come from createElementNS + setAttribute; labels and names via textContent.
    assert "document.createElementNS(SVG_NS, tag)" in script
    assert "el.setAttribute(k, String(v))" in script
    assert "b.textContent = best.s.name;" in script  # tooltip title
    assert "li.appendChild(document.createTextNode(s.name));" in script  # legend
    assert "b.textContent = String(q.symbol);" in script  # tape
    # The only innerHTML writes are static skeletons, the logo, the QR and the empty state.
    writes = re.findall(r"innerHTML = ([^;]+);", script)
    assert sorted(writes) == sorted(
        [
            "LOGO_SVG",
            'qrSVG(SUBMIT_URL, "#36182D", "#E320E7")',
            "'<li class=\"empty\">Scan to submit the first strategy</li>'",
            '""',
            '\'<span class="rk"></span><span class="sc num"></span><span class="nm"></span><span class="rt num"></span>\'',
        ]
    )


def test_chart_draws_the_start_reference_and_replay_label():
    assert 'rt.textContent = money(start) + " START";' in BOARD
    assert "stroke-dasharray: 6 6" in BOARD  # dashed $10,000 reference
    assert "REPLAY FEB 2-13 2026" in BOARD
    assert 'fetch("/api/tickers"' in BOARD
