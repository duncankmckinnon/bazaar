"""Counterfactual replay and paired strategy comparisons over independently accounted runs."""

from bazaar_replay.approval import ScopeCode, ScopedApproval, ScopeRejected, check_batch_scope
from bazaar_replay.baselines import BuyAndHold, CashOnly, DecisionPolicy, PriceLookup
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
    "BuyAndHold",
    "CashOnly",
    "ComparisonMismatch",
    "ComparisonSpec",
    "DecisionPolicy",
    "MatchedComparison",
    "MismatchCode",
    "PriceLookup",
    "RunDescriptor",
    "RunRole",
    "ScopeCode",
    "ScopeRejected",
    "ScopedApproval",
    "check_batch_scope",
]
