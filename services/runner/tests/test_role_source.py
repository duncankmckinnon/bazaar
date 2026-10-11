"""$BAZAAR_TRADING_ROLE_SOURCE: the managed role (default) or the reviewed code default."""

import pytest

pytest.importorskip("bazaar_agent.trading")

from bazaar_agent import trading
from bazaar_agent.trading import TRADING_ROLE, TRADING_ROLE_VARIABLE
from bazaar_runner import __main__ as cli
from bazaar_runner import role_source, submission
from bazaar_runner.role_source import ROLE_SOURCE_ENV, RoleSourceError
from logfire.variables.config import ResolvedVariable

from .test_demo import cli_ids
from .test_submission import GrantingMarket, markets, submit  # noqa: F401 - fixture

STALE = "OLD managed role: research at most 3 distinct symbols."


@pytest.fixture
def stale_provider(monkeypatch):
    """The Logfire provider holds an old managed value (stands in for the remote config)."""

    def lookup(name, **_):
        return ResolvedVariable(name=name, value=f'"{STALE}"', reason="resolved")

    monkeypatch.setattr(TRADING_ROLE_VARIABLE, "_lookup_serialized", lookup)
    refreshes = []
    real_refresh = TRADING_ROLE_VARIABLE.refresh

    async def refresh(force=False):
        refreshes.append(force)
        return await real_refresh(force=force)

    monkeypatch.setattr(TRADING_ROLE_VARIABLE, "refresh", refresh)
    return refreshes


@pytest.fixture
def roles_seen(monkeypatch):
    seen = []
    real = trading.run_decision

    async def spy(**kwargs):
        seen.append(kwargs["trading_role"])
        return await real(**kwargs)

    monkeypatch.setattr(trading, "run_decision", spy)
    return seen


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "managed"), ("", "managed"), ("managed", "managed"), ("code", "code")],
)
def test_the_source_comes_from_the_operator_env(value, expected):
    environ = {} if value is None else {ROLE_SOURCE_ENV: value}
    assert role_source.role_source(environ) == expected


@pytest.mark.parametrize("bad", ["Code", "logfire", "code; rm -rf /", "managed "])
def test_any_other_source_is_refused_unechoed(bad):
    with pytest.raises(RoleSourceError) as caught:
        role_source.role_source({ROLE_SOURCE_ENV: bad})
    message = str(caught.value)
    assert bad not in message and message.endswith("must be 'code' or 'managed'")


def run_span(capfire) -> dict:
    (span,) = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "runner.run"]
    return span["attributes"]


def test_managed_by_default_uses_the_managed_value(
    markets,  # noqa: F811 - the test_submission fixture
    tmp_path,
    monkeypatch,
    stale_provider,
    roles_seen,
    capfire,
):
    monkeypatch.delenv(ROLE_SOURCE_ENV, raising=False)
    markets["http://m1"] = GrantingMarket()
    submit(tmp_path, "http://m1")
    assert roles_seen == [STALE] * 10
    assert stale_provider == [True]
    assert run_span(capfire)["bazaar.trading_role_source"] == "managed"


def test_code_overrides_a_stale_managed_value_even_after_a_refresh(
    markets,  # noqa: F811 - the test_submission fixture
    tmp_path,
    monkeypatch,
    stale_provider,
    roles_seen,
    capfire,
):
    monkeypatch.setenv(ROLE_SOURCE_ENV, "code")
    markets["http://m1"] = GrantingMarket()
    submit(tmp_path, "http://m1")
    # The submission refreshed the variable (force=True) and still got the code default.
    assert stale_provider == [True]
    assert roles_seen == [TRADING_ROLE] * 10 and "at most 10 distinct symbols" in TRADING_ROLE
    attributes = run_span(capfire)
    assert attributes["bazaar.trading_role_source"] == "code"
    # The source's name only: no role text on the span or in the run files.
    assert TRADING_ROLE not in str(attributes) and STALE not in str(attributes)
    # Outside the submission, the override is gone.
    with TRADING_ROLE_VARIABLE.get() as resolved:
        assert resolved.value == STALE


def test_an_invalid_source_fails_the_submission_before_any_side_effect(
    markets,  # noqa: F811 - the test_submission fixture
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(ROLE_SOURCE_ENV, "remote")
    market = GrantingMarket()
    markets["http://m1"] = market
    configured = []
    monkeypatch.setattr(submission, "configure_telemetry", lambda: configured.append(1))
    with pytest.raises(submission.SubmissionFailed, match="trading role is not configured"):
        submit(tmp_path, "http://m1")
    assert market.paths == [] and configured == [] and list(tmp_path.iterdir()) == []


def test_the_cli_runs_every_launch_with_the_code_role(monkeypatch, stale_provider):
    monkeypatch.setenv(ROLE_SOURCE_ENV, "code")
    monkeypatch.setattr(cli, "configure_telemetry", lambda: None)
    seen = []

    async def amain(args):
        # Where the agent resolves its role per decision (run_decision with trading_role None).
        with TRADING_ROLE_VARIABLE.get() as resolved:
            seen.append((resolved.value, role_source.active_role_source()))
        return 0

    monkeypatch.setattr(cli, "amain", amain)
    assert cli.main(["--demo", *cli_ids()]) == 0
    assert seen == [(TRADING_ROLE, "code")]


def test_an_invalid_source_stops_the_cli_before_telemetry(monkeypatch, capsys):
    monkeypatch.setenv(ROLE_SOURCE_ENV, "remote")
    configured = []
    monkeypatch.setattr(cli, "configure_telemetry", lambda: configured.append(1))
    assert cli.main(["--demo", *cli_ids()]) == 2
    err = capsys.readouterr().err
    assert ROLE_SOURCE_ENV in err and "remote" not in err and configured == []
