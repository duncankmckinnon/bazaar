import re
from pathlib import Path

PAGE = Path(__file__).parents[1] / "src" / "bazaar_web" / "static" / "submit.html"


def page():
    return PAGE.read_text(encoding="utf-8")


def test_page_exists_with_a_viewport_meta():
    assert PAGE.is_file()
    assert re.search(r'<meta name="viewport" content="width=device-width', page())


def test_no_external_scripts_and_only_google_font_urls():
    html = page()

    assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.IGNORECASE)
    hosts = {
        re.match(r"https?://([^/\"'\s)]+)", url).group(1)
        for url in re.findall(r"https?://[^\s\"')]+", html)
    }
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}


def test_uses_the_submission_api_and_not_the_board_api():
    html = page()

    assert "/api/submissions" in html
    assert "/api/board" not in html


def test_validates_the_name_slug_and_strategy_length():
    html = page()

    assert "^[a-z0-9-]{3,40}$" in html
    assert re.search(r"MIN_CHARS = 20\b", html)
    assert re.search(r"MAX_CHARS = 4000\b", html)


def test_never_writes_html_from_data():
    html = page()

    assert not re.search(r"innerHTML\s*=", html)
    assert not re.search(r"outerHTML\s*=|insertAdjacentHTML|document\.write", html)
    assert not re.search(r"\beval\s*\(|new Function\s*\(", html)


def test_dwight_is_loaded_from_the_server_not_embedded():
    html = page()

    assert "data:font" not in html
    assert "/fonts/DwightMedium.woff2" in html
