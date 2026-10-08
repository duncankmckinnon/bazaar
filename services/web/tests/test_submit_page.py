import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def run_page_function(name, calls):
    """Run a small helper from the page's inline script in node and return its results."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    sources = re.findall(
        r"^  function (?:toNumber|percent|rankLabel)\(.*?^  }$", page(), re.MULTILINE | re.DOTALL
    )
    sources += re.findall(r"// safeLogfireUrl:start(.*?)// safeLogfireUrl:end", page(), re.DOTALL)
    script = (
        "\n".join(sources) + f"\nconsole.log(JSON.stringify([{', '.join(calls)}].map({name})));"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_percent_formats_numbers_and_decimal_strings():
    calls = ["0.6011", '"0.6011"', "-1.5", '"-1.5"', "0", "null", '""', '"abc"', "Infinity"]

    assert run_page_function("percent", calls) == [
        "+0.60%",
        "+0.60%",
        "-1.50%",
        "-1.50%",
        "0.00%",
        None,
        None,
        None,
        None,
    ]


def test_rank_label_accepts_positive_whole_numbers_and_digit_strings():
    calls = ["2", '"2"', "0", "1.5", "null", '"x"']

    assert run_page_function("rankLabel", calls) == ["#2", "#2", None, None, None, None]


def test_safe_logfire_url_allows_only_https_on_pydantic_dev():
    good = "https://logfire-us.pydantic.dev/x/y?q=1"
    calls = [
        json.dumps(good),
        "null",
        '""',
        '"javascript:alert(1)"',
        '"http://example.com"',
        '"/relative"',
        '"data:text/html,x"',
        "42",
        '"not a url"',
        '"HTTPS://logfire-us.pydantic.dev/x"',
        '"https://logfire-eu.pydantic.dev/a"',
        '"https://pydantic.dev/a"',
        '"https://evil.example/a"',
        '"https://pydantic.dev.evil.com/a"',
        '"https://logfire-us.pydantic.info/a"',
        '"https://notpydantic.dev/a"',
    ]

    assert run_page_function("safeLogfireUrl", calls) == [
        good,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "HTTPS://logfire-us.pydantic.dev/x",
        "https://logfire-eu.pydantic.dev/a",
        "https://pydantic.dev/a",
        None,
        None,
        None,
        None,
    ]


def test_logfire_link_is_labelled_and_opens_safely():
    html = page()

    assert "See your agent in Logfire" in html
    assert '"noopener noreferrer"' in html
    assert '"_blank"' in html
    assert "// safeLogfireUrl:start" in html and "// safeLogfireUrl:end" in html
