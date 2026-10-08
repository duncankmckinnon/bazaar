import dataclasses

import pytest
from bazaar_web.app import STATIC, create_app
from bazaar_web.settings import Settings
from fastapi.testclient import TestClient

TEMPLATE = "https://logfire.example/bazaar/dash?var-strategy={strategy}"
VALID = {"name": "alice-bot", "handle": None, "instructions": "Buy KO on dips, hold MSFT."}


def board_and_status(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        submission_id = client.post("/api/submissions", json=VALID).json()["id"]
        helpers.wait_for(
            lambda: client.get(f"/api/submissions/{submission_id}").json()["status"] == "scored"
        )
        board = client.get("/api/board").json()
        status = client.get(f"/api/submissions/{submission_id}").json()
        page = client.get("/").text
    return board, status, page


def test_unset_template_gives_null_links_and_the_same_page(seeded, helpers):
    board, status, page = board_and_status(seeded, helpers)
    linked = dataclasses.replace(seeded, logfire_dashboard_url=TEMPLATE)
    _, _, linked_page = board_and_status_fresh(linked, helpers)

    assert board["rows"]
    assert all(row["logfire_url"] is None for row in board["rows"])
    assert status["logfire_url"] is None
    assert page == linked_page  # the page never embeds the template


def board_and_status_fresh(settings, helpers, tmp_name="again"):
    fresh = dataclasses.replace(settings, web_db=settings.web_db.with_name(f"{tmp_name}.sqlite3"))
    return board_and_status(fresh, helpers)


def test_set_template_fills_every_row_and_the_status(seeded, helpers):
    board, status, _ = board_and_status(
        dataclasses.replace(seeded, logfire_dashboard_url=TEMPLATE), helpers
    )
    urls = {row["name"]: row["logfire_url"] for row in board["rows"]}

    assert urls["alice-bot"] == TEMPLATE.replace("{strategy}", "alice-bot")
    assert urls["baseline-buy-and-hold"] == TEMPLATE.replace("{strategy}", "baseline-buy-and-hold")
    assert status["logfire_url"] == urls["alice-bot"]


def test_strategy_name_is_percent_encoded():
    settings = Settings(logfire_dashboard_url=TEMPLATE)

    assert settings.logfire_url("a b/c?&<x>") == (
        "https://logfire.example/bazaar/dash?var-strategy=a%20b%2Fc%3F%26%3Cx%3E"
    )


def test_other_braces_in_the_template_survive():
    settings = Settings(
        logfire_dashboard_url='https://logfire.example/d?q={"name":"{strategy}"}&from={now-1h}'
    )

    assert settings.logfire_url("alice-bot") == (
        'https://logfire.example/d?q={"name":"alice-bot"}&from={now-1h}'
    )


@pytest.mark.parametrize(
    "template",
    [
        "https://logfire.example/dashboard",
        "javascript:alert('{strategy}')",
        "data:text/html,{strategy}",
        "ftp://logfire.example/{strategy}",
        "//logfire.example/{strategy}",
        "https:///{strategy}",
    ],
)
def test_bad_templates_are_rejected_at_startup(template):
    with pytest.raises(ValueError, match="BAZAAR_LOGFIRE_DASHBOARD_URL"):
        Settings(logfire_dashboard_url=template)


def test_bad_template_from_env_fails_create_app(monkeypatch):
    monkeypatch.setenv("BAZAAR_LOGFIRE_DASHBOARD_URL", "javascript:alert('{strategy}')")

    with pytest.raises(ValueError, match="http"):
        create_app()


def test_empty_env_var_means_unset(monkeypatch):
    monkeypatch.setenv("BAZAAR_LOGFIRE_DASHBOARD_URL", "")

    assert Settings.from_env().logfire_url("alice-bot") is None


def test_board_sets_the_link_through_dom_properties():
    board = (STATIC / "board.html").read_text(encoding="utf-8")
    script = board.split("<script>", 1)[1]

    assert 'a.setAttribute("href", url);' in script
    assert "a.textContent = name;" in script
    assert 'a.rel = "noopener noreferrer";' in script
    assert 'a.target = "_blank";' in script
    assert "if (!url) { b.textContent = name; return; }" in script
    assert 'setName(li.querySelector(".nm b"), r.name, safeUrl(r.logfire_url));' in script
    assert "logfire_url}" not in script  # never interpolated into an HTML string
    assert ".fl-row .nm b a { color: inherit; text-decoration: none;" in board
