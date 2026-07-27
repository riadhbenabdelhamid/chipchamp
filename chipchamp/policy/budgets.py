"""Per-task budgets (SPEC §12.4, FR-BUDG-01).

Exceeding a budget *pauses* the task and asks the human — it never silently
degrades a gate. The ledger is surfaced in the evidence bundle (cost accounting).
"""
from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """A budget ceiling was reached and the human did not authorize the overrun.

    Raised at the submission boundary (ToolContext.submit) rather than returned,
    so no caller can proceed by ignoring a return value — the point of a ceiling
    is that it cannot be walked past inattentively."""


@dataclass
class Budget:
    v3_submissions: int = 3
    license_hours: float = 24.0
    cpu_hours: float = 200.0
    model_tokens: int = 2_000_000


@dataclass
class BudgetLedger:
    budget: Budget = field(default_factory=Budget)
    v3_used: int = 0
    license_seconds: float = 0.0
    cpu_seconds: float = 0.0
    tokens_used: int = 0

    def can_submit_v3(self) -> tuple[bool, str]:
        if self.v3_used >= self.budget.v3_submissions:
            return False, (f"V3 submission budget exhausted "
                           f"({self.v3_used}/{self.budget.v3_submissions}) — pausing for human")
        return True, ""

    def can_spend_license(self, seconds: float) -> tuple[bool, str]:
        if (self.license_seconds + seconds) / 3600.0 > self.budget.license_hours:
            return False, "license-hour budget would be exceeded — pausing for human"
        return True, ""

    def can_spend_tokens(self, n: int = 0) -> tuple[bool, str]:
        """Checked before each model call. The model is told its budget in the
        system prompt, but telling a model its ceiling is not a ceiling — this
        is the part that actually holds."""
        if self.tokens_used + max(0, n) > self.budget.model_tokens:
            return False, (f"model-token budget exhausted "
                           f"({self.tokens_used}/{self.budget.model_tokens}) "
                           f"— pausing for human")
        return True, ""

    def record_job(self, rec) -> None:
        self.cpu_seconds += rec.cpu_seconds
        self.license_seconds += rec.license_seconds

    def record_v3(self) -> None:
        self.v3_used += 1

    def record_tokens(self, n: int) -> None:
        self.tokens_used += n

    def snapshot(self) -> dict:
        return {
            "v3_submissions": f"{self.v3_used}/{self.budget.v3_submissions}",
            "license_hours": f"{self.license_seconds/3600:.2f}/{self.budget.license_hours}",
            "cpu_hours": f"{self.cpu_seconds/3600:.3f}/{self.budget.cpu_hours}",
            "model_tokens": f"{self.tokens_used}/{self.budget.model_tokens}",
        }
