"""Policy engine (SPEC §8.2): the interceptor that makes trust structural.

Ties classification → gates → autonomy → budgets → ACL → anti-gaming together.
``check()`` answers the ``policy.check`` tool (what applies to this change);
``validate_done()`` is the backbone of ``report.done`` (FR-CORE-02): success may
be reported only when every required gate passes against real job records.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import brand
from . import antigaming
from .acl import ContextACL
from .autonomy import AutonomyDecision, decide
from .budgets import Budget, BudgetLedger
from .classify import classify
from .diff import Diff
from .gates import Evidence, GateReport, evaluate, required_gates


@dataclass
class PolicyCheck:
    task_class: str
    min_rung: str
    required_gates: list[str]
    autonomy: AutonomyDecision
    antigaming_findings: list[dict]
    acl_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"task_class": self.task_class, "min_rung": self.min_rung,
                "required_gates": self.required_gates,
                "autonomy": self.autonomy.__dict__,
                "antigaming_findings": self.antigaming_findings,
                "acl_notes": self.acl_notes}


class PolicyEngine:
    def __init__(self, *, acl: ContextACL | None = None,
                 autonomy_rules: dict | None = None, default_autonomy: str = "L1",
                 budget: Budget | None = None,
                 protected_paths: list[str] | None = None,
                 generated: list[dict] | None = None,
                 riscv_core: bool = False):
        self.acl = acl or ContextACL()
        # the workspace declares [riscv]: RTL edits are core edits, so ISA
        # conformance belongs inside 'done' (see classify)
        self.riscv_core = bool(riscv_core)
        self.autonomy_rules = autonomy_rules or {}
        self.default_autonomy = default_autonomy
        self.ledger = BudgetLedger(budget or Budget())
        # Every brand era, not just the current one: a workspace still on a
        # legacy dot-dir would otherwise have its policy file left writable
        # by the agent — a rename must not quietly widen what it may edit.
        self.protected_paths = protected_paths or (
            [f"{d}/policy.yaml" for d in brand.dot_dir_names()] + ["waivers/**"])
        # source-of-truth map entries: {outputs, sources, generator, kind} (SPEC §12.5)
        self.generated = generated or []

    # ---- read-side enforcement ---------------------------------------------

    def can_read(self, path: str) -> bool:
        return self.acl.is_allowed(path)

    def can_write(self, path: str) -> tuple[bool, str]:
        import fnmatch
        if not self.acl.is_allowed(path):
            return False, self.acl.reason(path)
        for g in self.protected_paths:
            if fnmatch.fnmatch(path, g):
                return False, f"protected path (policy) — '{g}' is not agent-editable"
        # FR-PROJ-04 / FR-DB-05: never hand-edit generated files — edit the source
        for entry in self.generated:
            for g in entry.get("outputs", []):
                if fnmatch.fnmatch(path, g):
                    return False, (
                        f"'{path}' is GENERATED (matches '{g}'). Edit the source of "
                        f"truth instead: {entry.get('sources')} then regenerate via: "
                        f"{entry.get('generator', 'regmap.generate')}")
        return True, "ok"

    # ---- policy.check -------------------------------------------------------

    def generated_globs(self) -> list[str]:
        return [g for e in self.generated for g in e.get("outputs", [])]

    def check(self, diff: Diff, closure_claim: bool = False) -> PolicyCheck:
        tclass = classify(diff, self.generated_globs(), self.riscv_core)
        rung, gates = required_gates(tclass)
        paths = [f.path for f in diff.files]
        autonomy = decide(tclass, paths, self.autonomy_rules, self.default_autonomy)
        findings = antigaming.scan(diff, closure_claim=closure_claim)
        acl_notes = [f"{f.path}: {self.acl.reason(f.path)}"
                     for f in diff.files if not self.acl.is_allowed(f.path)]
        return PolicyCheck(task_class=tclass, min_rung=rung, required_gates=gates,
                           autonomy=autonomy, antigaming_findings=findings,
                           acl_notes=acl_notes)

    # ---- report.done backbone ----------------------------------------------

    def validate_done(self, diff: Diff, evidence: Evidence,
                      closure_claim: bool = False) -> GateReport:
        tclass = classify(diff, self.generated_globs(), self.riscv_core)
        # merge freshly scanned anti-gaming findings with any the caller supplied
        scanned = antigaming.scan(diff, closure_claim=closure_claim)
        # a scanned finding is unacknowledged unless the caller marked it ack'd
        ack_index = {(f["kind"], f["file"]): f.get("acknowledged", False)
                     for f in evidence.antigaming}
        for f in scanned:
            f["acknowledged"] = ack_index.get((f["kind"], f["file"]), False)
        evidence.antigaming = scanned
        return evaluate(tclass, evidence)
