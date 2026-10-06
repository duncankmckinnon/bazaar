"""Counterfactual replay and paired strategy comparisons over independently accounted runs."""

from bazaar_replay.approval import ScopeCode, ScopedApproval, ScopeRejected, check_batch_scope
from bazaar_replay.comparison import (
    BatchKind,
    ComparisonMismatch,
    ComparisonSpec,
    MatchedComparison,
    MismatchCode,
    RunDescriptor,
    RunRole,
)

__all__ = [
    "BatchKind",
    "ComparisonMismatch",
    "ComparisonSpec",
    "MatchedComparison",
    "MismatchCode",
    "RunDescriptor",
    "RunRole",
    "ScopeCode",
    "ScopeRejected",
    "ScopedApproval",
    "check_batch_scope",
]
