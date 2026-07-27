"""Policy engine, gates, autonomy, budgets, ACLs, anti-gaming (SPEC §11)."""
from .acl import ContextACL
from .autonomy import AutonomyDecision, decide, resolve_level
from .budgets import Budget, BudgetExceeded, BudgetLedger
from .classify import classify
from .diff import Diff, FileChange
from .engine import PolicyCheck, PolicyEngine
from .gates import (
    CLASS_GATES,
    LADDER,
    Evidence,
    GateReport,
    GateResult,
    evaluate,
    required_gates,
)

__all__ = [
    "PolicyEngine",
    "PolicyCheck",
    "Diff",
    "FileChange",
    "classify",
    "Evidence",
    "GateReport",
    "GateResult",
    "evaluate",
    "required_gates",
    "LADDER",
    "CLASS_GATES",
    "ContextACL",
    "Budget",
    "BudgetExceeded",
    "BudgetLedger",
    "AutonomyDecision",
    "decide",
    "resolve_level",
]
