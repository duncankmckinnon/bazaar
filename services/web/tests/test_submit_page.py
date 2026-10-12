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


def test_the_only_script_src_is_the_same_origin_telemetry_module_in_the_head():
    html = page()
    tags = re.findall(r"<script\b[^>]*\bsrc\s*=[^>]*>", html, re.IGNORECASE)

    assert tags == ['<script type="module" src="/browser-telemetry.js">']
    head = html[: html.index("</head>")]
    assert '<script type="module" src="/browser-telemetry.js"></script>' in head


def test_no_external_scripts_and_only_google_font_urls():
    html = page()

    hosts = {
        re.match(r"https?://([^/\"'\s)]+)", url).group(1)
        for url in re.findall(r"https?://[^\s\"')]+", html)
    }
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}


def test_uses_the_submission_api_and_reads_the_board_once_for_its_window():
    html = page()

    assert "/api/submissions" in html
    assert html.count("/api/board") == 1  # a single fetch on load, for the trading window
    assert "setInterval" not in html


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
    sources += re.findall(r"// contactErrors:start(.*?)// contactErrors:end", page(), re.DOTALL)
    sources += re.findall(r"// windowText:start(.*?)// windowText:end", page(), re.DOTALL)
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


PAGE_BACKGROUNDS = ("#36182D", "#6a1a65", "#6b1b66")  # base and the blended magenta peak
FIELD_FILL = "#24101d"
TEXT_TOKENS = ("sugar", "aqua", "dim", "soft", "faint", "error")
PLACEHOLDER_TOKEN = "faint"


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


def test_text_tokens_meet_wcag_aa_on_the_page_and_the_input_fill():
    tokens = css_tokens()

    assert tokens["field"] == FIELD_FILL
    for name in TEXT_TOKENS:
        for background in (*PAGE_BACKGROUNDS, FIELD_FILL):
            assert contrast(tokens[name], background) >= 4.5, (name, background)


def test_placeholder_uses_a_pinned_token_that_meets_wcag_aa_on_the_fill():
    rule = re.search(r"::placeholder\s*\{([^}]*)\}", page()).group(1)

    assert f"color: var(--{PLACEHOLDER_TOKEN})" in rule
    assert "opacity: 1" in rule
    assert contrast(css_tokens()[PLACEHOLDER_TOKEN], FIELD_FILL) >= 4.5


def test_input_border_meets_non_text_contrast_against_page_and_field():
    tokens = css_tokens()

    for background in (*PAGE_BACKGROUNDS, FIELD_FILL):
        assert contrast(tokens["field-line"], background) >= 3, background
    assert re.search(
        r"input\[type=\"text\"\], input\[type=\"email\"\], textarea \{[^}]*border: 1px solid var\(--field-line\)",
        page(),
    )


def test_page_uses_the_board_background_and_no_low_contrast_text_colours():
    html = page()

    assert (
        "background: radial-gradient(60% 70% at 50% 18%, rgba(229,32,233,.30) 0%, "
        "rgba(229,32,233,0) 60%), radial-gradient(50% 60% at 90% 100%, rgba(255,101,80,.16) 0%, "
        "rgba(255,101,80,0) 60%), #36182D;"
    ) in html
    assert not re.search(r"(?<![-\w])color:\s*var\(--(?:calcium|lithium)\)", html)


GICS_SECTORS = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
)


def examples():
    """Each example's title and text, joined."""
    block = re.search(r"var EXAMPLES = \[(.*?)\n  \];", page(), re.DOTALL).group(1)
    return [" ".join(re.findall(r'"([^"]*)"', chunk)) for chunk in block.split("title:")[1:]]


def test_lede_offers_the_s_and_p_500_without_ticker_chips():
    html = page()
    lede_html = re.search(r'<p class="lede">(.*?)</p>', html, re.DOTALL).group(1)
    lede = re.sub(r"<[^>]+>", "", lede_html).replace("&amp;", "&")

    assert lede.startswith(
        "Your agent can trade any S&P 500 stock, using prices, news and filings, over "
    )
    assert 'class="ticker"' not in html and 'class="tickers"' not in html
    assert ".ticker" not in html
    assert "12 large US stocks" not in html


def test_examples_work_by_sector_or_screen_and_name_only_spy():
    found = examples()

    assert len(found) == 3
    for example in found:
        assert any(name in example for name in (*GICS_SECTORS, "S&P 500")), example
    words = re.findall(r"\b[A-Z]{1,5}\b", " ".join(found).replace("S&P 500", ""))
    assert set(words) <= {"SPY"}, set(words) - {"SPY"}


def input_tag(field_id):
    return re.search(rf'<input id="{field_id}"[^>]*>', page()).group(0)


def test_contact_inputs_are_required_with_autocomplete_and_limits():
    email, company, role = input_tag("email"), input_tag("company"), input_tag("role")

    for tag in (email, company, role):
        assert " required" in tag
    assert 'type="email"' in email and 'inputmode="email"' in email
    assert 'autocomplete="email"' in email and 'maxlength="254"' in email
    assert 'autocomplete="organization"' in company and 'maxlength="100"' in company
    assert 'autocomplete="organization-title"' in role and 'maxlength="100"' in role
    for field_id in ("email", "company", "role"):
        assert f'<label for="{field_id}">' in page()
        assert f'aria-describedby="contact-purpose {field_id}-error"' in input_tag(field_id)


def test_contact_purpose_line_is_shown():
    assert (
        "We'll use your email only to contact you if your strategy wins. Your company and role "
        "are used the same way and are never shown publicly."
    ) in " ".join(page().split())


def test_contact_errors_accepts_valid_contact_details():
    calls = [
        json.dumps({"email": "  ada@example.com ", "company": " Pydantic ", "role": "Engineer"}),
        json.dumps({"email": "a@b.co", "company": "x" * 100, "role": "y" * 100}),
        json.dumps({"email": "x" * 240 + "@example.com", "company": "C", "role": "R"}),
    ]

    assert (
        run_page_function("contactErrors", calls) == [{"email": "", "company": "", "role": ""}] * 3
    )


def test_contact_errors_rejects_invalid_contact_details():
    long_email = "x" * 243 + "@example.com"  # 255 characters
    calls = [
        json.dumps({"email": "ada.example.com", "company": "C", "role": "R"}),
        json.dumps({"email": "ada @example.com", "company": "C", "role": "R"}),
        json.dumps({"email": "ada@example", "company": "C", "role": "R"}),
        json.dumps({"email": long_email, "company": "C", "role": "R"}),
        json.dumps({"email": "", "company": "   ", "role": ""}),
        json.dumps({"email": "a@b.co", "company": "x" * 101, "role": "y" * 101}),
        "{}",
    ]
    results = run_page_function("contactErrors", calls)

    for result in results[:4]:
        assert result["email"] and not result["company"] and not result["role"]
    assert all(results[4].values())
    assert not results[5]["email"] and results[5]["company"] and results[5]["role"]
    assert all(results[6].values())


def test_local_storage_keeps_only_id_name_and_position():
    html = page()
    saved = re.findall(r"var saved = \{([^}]*)\}", html)
    store = re.search(r"var store = \{(.*?)\n  \};", html, re.DOTALL).group(1)

    assert saved, "the page should save its submission"
    for literal in saved:
        assert re.findall(r"(\w+):", literal) == ["id", "name", "position"]
    assert re.findall(r"store\.set\(([^)]*)\)", html) == ["saved"]
    assert (
        html.count("localStorage")
        == html.count("window.localStorage")
        == store.count("localStorage")
    )
    assert not re.search(r"email|company|role", store)
    assert "console." not in html


def test_contact_details_never_reach_the_url_and_422_input_is_never_shown():
    html = page()

    assert '<form id="submit-form" method="post"' in html
    for field_id in ("email", "company", "role"):
        assert " name=" not in input_tag(field_id), field_id
    show_detail = re.search(r"function showDetail\(.*?\n  \}\n", html, re.DOTALL).group(0)
    assert set(re.findall(r"\bitem\.(\w+)", show_detail)) == {"msg", "loc"}
    assert not re.search(r"\.input\b", html)


def test_server_field_errors_map_to_the_contact_fields():
    fields = re.search(r"var fields = \{([^}]*)\}", page()).group(1)

    for name in ("name", "handle", "instructions", "email", "company", "role"):
        assert re.search(rf"\b{name}: ", fields), name


def test_window_text_formats_the_api_window():
    calls = [
        json.dumps({"start": "2026-09-28", "end": "2026-10-09", "days": 10}),
        json.dumps({"start": "2025-12-29", "end": "2026-01-09", "days": 10}),
        json.dumps({"start": "2026-09-28", "end": "2026-10-09"}),
        json.dumps({"start": "2026-09-28", "end": "2026-10-09", "days": 5}),
        json.dumps({"start": "2026-09-28", "end": "2026-10-09", "days": 0}),
        json.dumps({"start": "2026-09-28", "end": "2026-10-09", "days": "7"}),
        json.dumps({"start": "2026-10-09", "end": "2026-10-09", "days": 1}),
        json.dumps({"start": "2024-02-29", "end": "2024-03-01", "days": 2}),
    ]

    assert run_page_function("windowText", calls) == [
        "10 trading days, Sep 28 to Oct 9 2026",
        "10 trading days, Dec 29 2025 to Jan 9 2026",
        "10 trading days, Sep 28 to Oct 9 2026",
        "5 trading days, Sep 28 to Oct 9 2026",
        "10 trading days, Sep 28 to Oct 9 2026",
        "10 trading days, Sep 28 to Oct 9 2026",
        "1 trading day, Oct 9 to Oct 9 2026",
        "2 trading days, Feb 29 to Mar 1 2024",
    ]


def test_window_text_rejects_invalid_windows():
    calls = [
        "null",
        "{}",
        json.dumps({"start": "2026-13-01", "end": "2026-12-31"}),
        json.dumps({"start": "2026-02-01", "end": "2026-02-30"}),
        json.dumps({"start": "2025-02-29", "end": "2025-03-01"}),
        json.dumps({"start": "2026-10-09", "end": "2026-09-28"}),
        json.dumps({"start": 20260928, "end": "2026-10-09"}),
        json.dumps({"start": "2026-9-28", "end": "2026-10-09"}),
        json.dumps({"start": "2026-09-28T00:00:00Z", "end": "2026-10-09"}),
        '"2026-09-28"',
    ]

    assert run_page_function("windowText", calls) == [None] * len(calls)


def test_lede_markup_has_no_hard_coded_dates():
    lede = re.search(r'<p class="lede">(.*?)</p>', page(), re.DOTALL).group(1)

    assert "Feb 2" not in lede and "2026" not in lede
    assert "10 recent trading days" in lede
    assert "// windowText:start" in page() and "// windowText:end" in page()
