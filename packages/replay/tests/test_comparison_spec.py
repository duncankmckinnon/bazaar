from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from bazaar_replay import (
    BatchKind,
    ComparisonMismatch,
    ComparisonSpec,
    MismatchCode,
    RunDescriptor,
    ScopeCode,
    ScopedApproval,
    ScopeRejected,
    check_batch_scope,
)
from pydantic import ValidationError

START = datetime(2025, 7, 1, 20, tzinfo=UTC)
END = datetime(2026, 9, 30, 20, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
CANDIDATE_TOOLS = frozenset({"account", "market_history", "orders", "news"})


def run(**updates):
    values = {
        "role": "candidate",
        "policy_ref": "alpha-trader@v1",
        "start_at": START,
        "end_at": END,
        "starting_capital": "100000",
        "data_version": "fixture-2025q3-v1",
        "execution_rule_version": "immediate-v1",
        "evaluator_version": "evals-v1",
        "mark_schedule_digest": "sha256:daily-close-fixture",
        "tools": CANDIDATE_TOOLS,
        "budget": "25",
        "seed": 7,
        "repetition": 0,
    }
    return RunDescriptor(**(values | updates))


def baseline(policy_ref, **updates):
    values = {"role": "baseline", "policy_ref": policy_ref, "tools": frozenset(), "budget": "0"}
    return run(**(values | updates))


def matched_runs():
    return (run(), baseline("cash-only"), baseline("buy-and-hold-equal-weight"))


def spec(*runs):
    return ComparisonSpec(runs=runs or matched_runs())


def grant(**updates):
    values = {
        "approval_id": UUID("00000000-0000-0000-0000-000000000018"),
        "kind": "comparison",
        "start_at": START,
        "end_at": END,
        "data_version": "fixture-2025q3-v1",
        "execution_rule_version": "immediate-v1",
        "evaluator_version": "evals-v1",
        "candidates": frozenset({"alpha-trader@v1"}),
        "baselines": frozenset({"cash-only", "buy-and-hold-equal-weight"}),
        "run_limit": 3,
        "expires_at": NOW + timedelta(days=1),
    }
    return ScopedApproval(**(values | updates))


def test_baseline_without_tools_matches_candidate_with_tools():
    matched = spec().validate_matched()

    assert matched.repetitions == 1
    assert matched.candidates == ("alpha-trader@v1",)
    assert matched.baselines == ("buy-and-hold-equal-weight", "cash-only")
    assert matched.access_budget_exempt == ("buy-and-hold-equal-weight", "cash-only")


def test_matched_repetitions_and_baseline_with_candidate_access_is_not_exempt():
    runs = (
        run(),
        run(repetition=1, seed=8),
        baseline("cash-only"),
        baseline("cash-only", repetition=1),
        baseline("buy-and-hold-equal-weight", tools=CANDIDATE_TOOLS, budget="25"),
        baseline("buy-and-hold-equal-weight", tools=CANDIDATE_TOOLS, budget="25", repetition=1),
    )
    matched = spec(*runs).validate_matched()

    assert matched.repetitions == 2
    assert matched.access_budget_exempt == ("cash-only",)


def test_two_candidates_with_equal_access_match():
    matched = spec(
        run(), run(policy_ref="beta-trader@v1"), baseline("cash-only")
    ).validate_matched()

    assert matched.candidates == ("alpha-trader@v1", "beta-trader@v1")


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"starting_capital": "50000"}, MismatchCode.STARTING_CAPITAL),
        ({"start_at": START + timedelta(days=1)}, MismatchCode.PERIOD_START),
        ({"end_at": END - timedelta(days=1)}, MismatchCode.PERIOD_END),
        ({"data_version": "fixture-2025q3-v2"}, MismatchCode.DATA_VERSION),
        ({"execution_rule_version": "immediate-v2"}, MismatchCode.EXECUTION_RULE_VERSION),
        ({"evaluator_version": "evals-v2"}, MismatchCode.EVALUATOR_VERSION),
        ({"mark_schedule_digest": "sha256:weekly-close"}, MismatchCode.MARK_SCHEDULE),
    ],
)
def test_shared_field_mismatch_is_rejected_for_any_run(updates, code):
    # Baselines are exempt from access/budget only; they still share everything else.
    with pytest.raises(ComparisonMismatch) as exc:
        spec(run(), baseline("cash-only", **updates)).validate_matched()

    assert exc.value.code is code


def test_capital_compares_by_amount_not_spelling():
    assert spec(run(), baseline("cash-only", starting_capital="100000.00")).validate_matched()


def test_unequal_repetition_counts_are_rejected():
    runs = (run(), run(repetition=1, seed=8), baseline("cash-only"))

    with pytest.raises(ComparisonMismatch) as exc:
        spec(*runs).validate_matched()

    assert exc.value.code is MismatchCode.REPETITION_COUNT


@pytest.mark.parametrize(
    ("baseline_repetitions", "candidate_repetitions"),
    [
        pytest.param((0, 5), (0, 1), id="unpaired-index"),
        pytest.param((1, 2), (1, 2), id="not-from-zero"),
    ],
)
def test_repetition_indices_must_be_zero_to_n(baseline_repetitions, candidate_repetitions):
    runs = (
        *(run(repetition=i, seed=i) for i in candidate_repetitions),
        *(baseline("cash-only", repetition=i) for i in baseline_repetitions),
    )

    with pytest.raises(ComparisonMismatch) as exc:
        spec(*runs).validate_matched()

    assert exc.value.code is MismatchCode.REPETITION_INDEX


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"tools": frozenset({"account", "market_history", "orders"})}, MismatchCode.ACCESS),
        ({"budget": "50"}, MismatchCode.BUDGET),
    ],
)
def test_candidate_access_or_budget_mismatch_across_candidates(updates, code):
    with pytest.raises(ComparisonMismatch) as exc:
        spec(run(), run(policy_ref="beta-trader@v1", **updates)).validate_matched()

    assert exc.value.code is code


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"tools": frozenset({"account"})}, MismatchCode.ACCESS),
        ({"budget": "10"}, MismatchCode.BUDGET),
    ],
)
def test_candidate_access_or_budget_mismatch_across_repetitions(updates, code):
    runs = (
        run(),
        run(repetition=1, seed=8, **updates),
        baseline("cash-only"),
        baseline("cash-only", repetition=1),
    )

    with pytest.raises(ComparisonMismatch) as exc:
        spec(*runs).validate_matched()

    assert exc.value.code is code


@pytest.mark.parametrize(
    "runs",
    [
        pytest.param((run(),), id="single-run"),
        pytest.param((baseline("cash-only"), baseline("buy-and-hold")), id="no-candidate"),
        pytest.param((run(), run(seed=8), baseline("cash-only")), id="duplicate-repetition"),
        pytest.param((run(), baseline("alpha-trader@v1", repetition=1)), id="policy-in-two-roles"),
    ],
)
def test_malformed_spec_is_rejected(runs):
    with pytest.raises(ValidationError):
        ComparisonSpec(runs=runs)


def test_run_period_must_be_ordered_utc():
    with pytest.raises(ValidationError):
        run(end_at=START)
    with pytest.raises(ValidationError):
        run(start_at=START.astimezone(timezone(timedelta(hours=-4))))
    with pytest.raises(ValidationError):
        run(starting_capital=100000.0)
    with pytest.raises(ValidationError):
        run(mark_schedule_digest="")


def test_grant_covering_the_batch_passes():
    check_batch_scope(grant(), spec(), now=NOW)
    check_batch_scope(grant(run_limit=10, runs_consumed=7), spec(), now=NOW)


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"kind": BatchKind.REPLAY}, ScopeCode.WRONG_KIND),
        ({"run_limit": 2}, ScopeCode.SCOPE_EXCEEDED),
        ({"run_limit": 5, "runs_consumed": 3}, ScopeCode.SCOPE_EXCEEDED),
        ({"start_at": START - timedelta(days=1)}, ScopeCode.PERIOD),
        ({"end_at": END + timedelta(days=1)}, ScopeCode.PERIOD),
        ({"data_version": "fixture-2025q3-v2"}, ScopeCode.VERSION),
        ({"execution_rule_version": "immediate-v2"}, ScopeCode.VERSION),
        ({"evaluator_version": "evals-v2"}, ScopeCode.VERSION),
        ({"candidates": frozenset({"beta-trader@v9"})}, ScopeCode.CANDIDATES),
        ({"candidates": frozenset({"alpha-trader@v1", "beta-trader@v1"})}, ScopeCode.CANDIDATES),
        ({"baselines": frozenset({"cash-only"})}, ScopeCode.BASELINES),
        ({"expires_at": NOW}, ScopeCode.EXPIRED),
        ({"revoked_at": NOW - timedelta(hours=1)}, ScopeCode.REVOKED),
    ],
)
def test_grant_rejections(updates, code):
    with pytest.raises(ScopeRejected) as exc:
        check_batch_scope(grant(**updates), spec(), now=NOW)

    assert exc.value.code is code


def test_expiry_uses_the_clock_passed_in():
    approval = grant(expires_at=NOW)

    check_batch_scope(approval, spec(), now=NOW - timedelta(seconds=1))
    with pytest.raises(ScopeRejected):
        check_batch_scope(approval, spec(), now=NOW)


def test_grant_for_one_candidate_does_not_authorize_another():
    other = spec(
        run(policy_ref="beta-trader@v9"),
        baseline("cash-only"),
        baseline("buy-and-hold-equal-weight"),
    )

    with pytest.raises(ScopeRejected) as exc:
        check_batch_scope(grant(), other, now=NOW)

    assert exc.value.code is ScopeCode.CANDIDATES


def test_unmatched_spec_is_rejected_before_scope():
    unmatched = spec(run(), baseline("cash-only", data_version="fixture-2025q3-v2"))

    with pytest.raises(ComparisonMismatch):
        check_batch_scope(grant(baselines=frozenset({"cash-only"})), unmatched, now=NOW)


def test_now_must_be_utc():
    with pytest.raises(ValueError, match="UTC"):
        check_batch_scope(grant(), spec(), now=NOW.replace(tzinfo=None))


def test_grant_cannot_be_overconsumed():
    with pytest.raises(ValidationError):
        grant(run_limit=1, runs_consumed=2)
