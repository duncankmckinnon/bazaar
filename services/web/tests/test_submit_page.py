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


PAGE_BACKGROUNDS = ("#36182D", "#6a1a65")  # base and the blended magenta peak
FIELD_FILL_TOKEN = "field"
TEXT_TOKENS = ("sugar", "aqua", "dim", "soft", "faint", "error")


def css_tokens():
    root = re.search(r":root\s*\{(.*?)\}", page(), re.DOTALL).group(1)
    return {
        name: value.lower() for name, value in re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", root)
    }


def contrast(a, b):
    def luminance(color):
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_text_tokens_meet_wcag_aa_on_the_page_background():
    tokens = css_tokens()

    for name in TEXT_TOKENS:
        for background in PAGE_BACKGROUNDS:
            assert contrast(tokens[name], background) >= 4.5, (name, background)


def test_input_border_meets_non_text_contrast_against_page_and_field():
    tokens = css_tokens()

    for background in (*PAGE_BACKGROUNDS, tokens[FIELD_FILL_TOKEN]):
        assert contrast(tokens["field-line"], background) >= 3, background
    assert re.search(
        r"input\[type=\"text\"\], textarea \{[^}]*border: 1px solid var\(--field-line\)", page()
    )


def test_page_uses_the_board_background_and_no_low_contrast_text_colours():
    html = page()

    assert (
        "background: radial-gradient(60% 70% at 50% 18%, rgba(229,32,233,.30) 0%, "
        "rgba(229,32,233,0) 60%), radial-gradient(50% 60% at 90% 100%, rgba(255,101,80,.16) 0%, "
        "rgba(255,101,80,0) 60%), #36182D;"
    ) in html
    assert not re.search(r"(?<![-\w])color:\s*var\(--(?:calcium|lithium)\)", html)
