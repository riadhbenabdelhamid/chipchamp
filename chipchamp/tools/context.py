"""Tool execution context — the live services + per-task state a tool operates on.

Holds the workspace-constructed services (design DB, adapter registry, job
runner, policy engine, coverage) plus the mutable state of the current task:
jobs run so far (the evidence pool that ``report.done`` validates), open wave
stores, named coverage sets and the budget ledger.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from ..config import Workspace
from ..coverage import CoverageService
from ..policy.budgets import BudgetExceeded
from ..waves import WaveStore


class ToolContext:
    def __init__(self, ws: Workspace, target: Optional[str] = None,
                 runner=None, policy=None):
        """`runner`/`policy` may be shared from a parent context — subagents
        (SPEC §8.10) must share the parent's job store, license-token pool and
        budget ledger so fan-out cannot bypass FR-MA-02/FR-BUDG-01."""
        self.ws = ws
        self.target_name = target or ws.default_target
        self.registry = ws.registry()
        self.runner = runner if runner is not None else ws.runner()
        self.policy = policy if policy is not None else ws.policy_engine()
        self.ledger = self.policy.ledger
        self.target = ws.target(self.target_name)
        # per-task state
        self.task_jobs: list = []
        self.open_waves: dict[str, WaveStore] = {}
        self.coverage_sets: dict[str, CoverageService] = {}
        self.log_lines: list[str] = []
        # edit tracking, so a Diff can be reconstructed for the gates
        self.edits: dict[str, dict] = {}  # rel_path -> {old, new, status}
        self.sta_delta: dict | None = None  # set by sta.run w/ baseline (timing gate)
        # RISC-V ISA evidence (rung R): set by riscv.cosim / riscv.compliance
        self.riscv_cosim: dict | None = None
        self.riscv_compliance: dict | None = None
        self.plan: list[dict] = []  # the agent's live todo checklist (§8.2)
        self.declared_nfc = False
        self.declared_cdc = False
        self.declared_timing = False
        self.closure_claim = False
        self.acked_findings: set[tuple] = set()  # (kind, file) human-acknowledged
        # asked when a budget would be exceeded; the REPL wires a prompt, and
        # the default refuses so headless runs stop at their ceiling
        self.budget_approver = lambda kind, why: False
        # undo stack: checkpoints of the edit set, newest last
        self.undo_stack: list[dict] = []
        # detached job ids still running when the task ends — the session
        # parks on these so a later wake can collect them
        self.pending_jobs: list[str] = []
        self._history = None
        self._notebook = None

    # ---- lazy design db -----------------------------------------------------

    @property
    def db(self):
        return self.ws.db(self.target_name)

    @property
    def notebook(self):
        """Durable findings across sessions. Lazy for the same reason history
        is: a workspace nobody has taught anything stays a workspace with no
        notebook file."""
        if self._notebook is None:
            from ..notebook import Notebook
            self._notebook = Notebook(self.ws.dot)
        return self._notebook

    @property
    def history(self):
        """Cross-run test outcomes (flake detection). Lazy: a workspace that
        never runs a sim never grows the file."""
        if self._history is None:
            from ..jobs.history import TestHistory
            self._history = TestHistory(self.ws.dot)
        return self._history

    def note(self, msg: str) -> None:
        self.log_lines.append(msg)

    # ---- job submission with budget/evidence bookkeeping --------------------

    def submit(self, plan, adapter, *, is_v3: bool = False, seed=None,
               input_files=None, timeout=None, no_cache: bool = False,
               detach: bool = False):
        """Launch a job, after the budget says it may be launched.

        The budget check happens HERE rather than inside the runner because
        this is the boundary the agent crosses: a playbook or a human running
        `chipchamp sim` is spending their own time knowingly, while the agent
        spending the twentieth licensed hour of an unattended night is exactly
        what FR-BUDG-01 exists to interrupt.

        Exceeding a budget raises :class:`BudgetExceeded` unless the human
        approves the overrun — a pause, never a silent degrade."""
        if is_v3:
            ok, why = self.ledger.can_submit_v3()
            if not ok and not self._budget_ok("v3_submissions", why):
                raise BudgetExceeded(why)
        if plan.license_features:
            ok, why = self.ledger.can_spend_license(0.0)
            if not ok and not self._budget_ok("license_hours", why):
                raise BudgetExceeded(why)
        kw = dict(input_files=input_files or self.target.sources, seed=seed,
                  timeout=timeout,
                  env_modules=self.ws.config.get("tools", {})
                  .get("env_modules", {}).get(adapter.name, []))
        if detach:
            # FR-JOB-04: hand back a running record and let the caller get on
            # with something else. No cache lookup — reuse is instant anyway,
            # so anything worth detaching is by definition not a hit.
            rec = self.runner.submit_async(plan, adapter, **kw)
            self.task_jobs.append(rec)
            self.pending_jobs.append(rec.id)
            return rec, None
        rec, result = self.runner.submit(plan, adapter, no_cache=no_cache, **kw)
        self.task_jobs.append(rec)
        if getattr(rec, "cached", False):
            # a reused verdict costs nothing and must not be billed as if it
            # had run — and the human is told, because an invisible cache is
            # indistinguishable from a lie about work performed
            self.note(f"{rec.id} reused (identical inputs — nothing re-run)")
        else:
            self.ledger.record_job(rec)
            if is_v3:
                self.ledger.record_v3()
        return rec, result

    def _budget_ok(self, kind: str, why: str) -> bool:
        """Ask the human to authorize an overrun. Default is refusal: an
        unattended run must stop at its ceiling, not sail past it because
        nobody was watching."""
        try:
            return bool(self.budget_approver(kind, why))
        except Exception:
            return False

    # ---- helpers ------------------------------------------------------------

    def workdir(self) -> str:
        return self.ws.root

    def rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.ws.root)
        except ValueError:
            return path

    def resolve_wave(self, ref: str) -> Optional[WaveStore]:
        if ref in self.open_waves:
            return self.open_waves[ref]
        # ref may be a job id
        rec = self.runner.get(ref)
        if rec and rec.artifacts.get("waves") and os.path.exists(rec.artifacts["waves"]):
            ws = WaveStore.open(rec.artifacts["waves"], provenance=rec.id)
            self.open_waves[ref] = ws
            return ws
        return None

    def find_test(self, name: str) -> Optional[dict]:
        return next((t for t in self.ws.tests if t.get("name") == name), None)

    # ---- edit tracking ------------------------------------------------------

    def record_edit(self, rel_path: str, old: str, new: str, status: str) -> None:
        if rel_path in self.edits:
            self.edits[rel_path]["new"] = new
            self.edits[rel_path]["status"] = status
        else:
            self.edits[rel_path] = {"old": old, "new": new, "status": status}

    def gate_status(self):
        """The live gate report for the current change set, or None.

        Same evidence assembly `report.done` validates against — so the ladder
        a human watches during a task is the ladder the task will be judged by,
        never an optimistic approximation of it. Best-effort: this runs after
        every job for a HUD, and a display must not be able to fail a run."""
        try:
            from .meta_tools import _collect_evidence
            return self.policy.validate_done(
                self.current_diff(), _collect_evidence(self, []),
                closure_claim=self.closure_claim)
        except Exception:
            return None

    # ---- undo ---------------------------------------------------------------
    #
    # `edits` already holds {old, new} per path — a complete inverse patch for
    # the session, kept so the gates can reconstruct a Diff. It was never
    # reachable as an action, so a bad edit could only be undone by hand. In
    # RTL that hurts more than in software: a wrong edit is often forty minutes
    # of resimulation away from being noticed, and by then it is tangled up
    # with three good ones. Checkpoints scope the revert to one task.

    def checkpoint(self, label: str = "") -> None:
        """Mark the current edit state, so `undo` can return to exactly here."""
        self.undo_stack.append({
            "label": label, "ts": _now(),
            "files": {p: e["new"] for p, e in self.edits.items()}})

    def undo(self) -> dict:
        """Restore the tree to the last checkpoint. Returns what moved.

        Files created after the checkpoint are deleted; files modified are
        rewritten to their contents at checkpoint time. A path whose current
        on-disk content no longer matches what the agent last wrote is left
        alone and reported: somebody edited it by hand since, and silently
        discarding a human's work would be the worst possible undo."""
        if not self.undo_stack:
            return {"error": "nothing to undo"}
        cp = self.undo_stack.pop()
        restored, deleted, skipped = [], [], []
        for path, e in sorted(self.edits.items()):
            tracked = path in cp["files"]
            # A path absent from the checkpoint was FIRST touched during the
            # task being undone — so its pre-task content is the `old` the very
            # first edit recorded. Treating absent as "did not exist" would
            # delete a file the task merely modified.
            was = cp["files"][path] if tracked else e["old"]
            if was == e["new"]:
                continue                      # unchanged since the checkpoint
            full = Path(self.ws.root) / path
            try:
                on_disk = full.read_text() if full.exists() else None
            except OSError:
                on_disk = None
            if on_disk is not None and on_disk != e["new"]:
                skipped.append(path)          # a human touched it — hands off
                continue
            # created by this task (fs.write records "added" when nothing was
            # there) → the way back is removal, not an empty file
            created = not tracked and e["status"] == "added"
            try:
                if created:
                    if full.exists():
                        full.unlink()
                    deleted.append(path)
                    self.edits.pop(path, None)
                else:
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_text(was)
                    restored.append(path)
                    self.edits[path]["new"] = was
            except OSError as err:
                skipped.append(f"{path} ({err})")
        return {"checkpoint": cp.get("label", ""), "restored": restored,
                "deleted": deleted, "skipped": skipped}

    def current_diff(self):
        from ..policy import Diff, FileChange
        files = [FileChange(path=p, status=e["status"], old_text=e["old"],
                            new_text=e["new"]) for p, e in self.edits.items()]
        return Diff(files=files, declared_nfc=self.declared_nfc,
                    declared_cdc=self.declared_cdc, declared_timing=self.declared_timing)


def _now() -> float:
    return time.time()
