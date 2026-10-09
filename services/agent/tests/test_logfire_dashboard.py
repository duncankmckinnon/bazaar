import json
from pathlib import Path

DASHBOARD = Path(__file__).parents[3] / "infra" / "logfire" / "bazaar-strategy.json"


def query(definition, panel):
    return definition["spec"]["panels"][panel]["spec"]["queries"][0]["spec"]["plugin"][
        "spec"
    ]["query"]


def test_dashboard_reports_raw_strategy_adherence_probability():
    definition = json.loads(DASHBOARD.read_text())

    assert "strategy_adherence_probability" in query(definition, "adherence")
    assert "strategy_adherence_probability" in query(definition, "all-strategies")
    assert "strategy_adherence_probability" in query(definition, "adherence-detail")
    assert "strategy_adherence_confidence" not in DASHBOARD.read_text()

