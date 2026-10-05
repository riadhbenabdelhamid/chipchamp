"""The state of one agent run, in three objects instead of ninety locals.

run() used to hold every counter, set and flag of a run as a local variable
— about thirty of them, shared by a dozen inlined guards — plus closures
that read them. Nothing could be tested on its own. The same facts now live
in:

- RunState — the per-run counters and flags (how many empty turns, is the
  task engaged, what did the last lint say).
- WorkspaceLedger — what THIS run wrote and deleted, and the tree epoch that
  every verification result is stamped with. It renders the ledger turn the
  model is told to trust over its memory.
- VerificationTracker — every check's latest result per scope, stamped with
  the epoch it ran on; whether the tree is green right now; the green
  checkpoint and the failure count since it. observe() applies one tool
  result and returns the teachings that result earns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import teachings

LEDGER_TAG = "[workspace ledger]"


@dataclass
class RunState:
    final_text: str = ""
    budget_stop: str = ""
    step: int = 0
    empty_turns: int = 0
    length_stops: int = 0
    recovered_calls: int = 0
    call_retries: int = 0
    narration_nudges: int = 0      # consecutive text-only turns nudged to act
    writes_since_verify: int = 0   # authored files since the last lint/sim job
    verify_nudged_at: int = 0      # writes count at the last cadence teaching
    reported_done: bool = False    # report.done accepted → text is a real answer
    made_mutating_call: bool = False  # wrote a file / ran a job → needs report.done
    last_reasoning: str = ""
    stall: int = 0                 # consecutive failing checks with no progress
    last_lint_err: Optional[int] = None
    sim_sig: Optional[tuple] = None    # (test, normalized summary) of last sim fail
    sim_sig_count: int = 0
    sim_sigs_nudged: set = field(default_factory=set)
    idle_turns: int = 0            # consecutive turns with no constructive call
    progress_taught: bool = False

    def saw_sim_signature(self, sig: Optional[tuple], job_passed: bool) -> None:
        """Track identical consecutive sim failures (the bad-oracle symptom)."""
        if sig is not None:
            if sig == self.sim_sig:
                self.sim_sig_count += 1
            else:
                self.sim_sig, self.sim_sig_count = sig, 1
        elif job_passed:
            self.sim_sig, self.sim_sig_count = None, 0

    def oracle_suspect(self) -> bool:
        return (self.sim_sig is not None and self.sim_sig_count >= 3
                and self.sim_sig not in self.sim_sigs_nudged)


class WorkspaceLedger:
    """Files this run authored and deleted, and the tree epoch."""

    def __init__(self) -> None:
        self.authored: set = set()
        self.deleted: set = set()
        self.epoch = 0           # bumps on every mutation of the tree
        self.active = False      # once anything was evicted, keep it current

    def wrote(self, path: str) -> None:
        if path:
            self.authored.add(path)
            self.deleted.discard(path)

    def removed(self, path: str) -> None:
        if path:
            self.authored.discard(path)
            self.deleted.add(path)

    def bump(self) -> None:
        self.epoch += 1

    @property
    def dirty(self) -> bool:
        return bool(self.authored or self.deleted)

    def render(self, verify_summary: str) -> str:
        return (f"{LEDGER_TAG} Authoritative record kept by the loop — "
                f"trust it over your memory. Files you authored that "
                f"still exist: {', '.join(sorted(self.authored)) or '(none)'}. "
                f"Files you deleted: {', '.join(sorted(self.deleted)) or '(none)'}. "
                f"Verification on the CURRENT tree: {verify_summary}. "
                f"A file not listed as authored does NOT exist — fs.list "
                f"before assuming.")

    @staticmethod
    def strip(transcript: list) -> None:
        """Remove the ledger turn, in place (the model sees exactly one)."""
        transcript[:] = [m for m in transcript if not (
            m.get("role") == "user"
            and str(m.get("content", "")).startswith(LEDGER_TAG))]

    def refresh(self, transcript: list, verify_summary: str) -> None:
        """Replace the ledger turn with a current one, in place."""
        self.strip(transcript)
        transcript.append({"role": "user", "guard": "ledger",
                           "content": self.render(verify_summary)})


class VerificationTracker:
    """Latest result per (tool, scope), stamped with the epoch it ran on."""

    def __init__(self) -> None:
        self.checks: dict = {}         # (tool, scope) -> result record
        self.green_epoch = -1          # epoch at which the tree was last green
        self.green_job = ""            # strongest passing job at that green
        self.fails_since_green = 0
        self.checkpoint_taught = False
        self.scope_taught: set = set()  # (key, epoch, kind) already taught

    def current(self, epoch: int) -> list[dict]:
        return [v for v in self.checks.values() if v["epoch"] == epoch]

    def summary(self, epoch: int) -> str:
        cur = self.current(epoch)
        if not cur:
            return "none yet on this tree (re-run your checks after edits)"
        return "; ".join(
            f"{v['tool']}({v['scope']}) {v['job']} "
            + ("passed" if v["passed"] else
               (f"FAILED ({v['errors']} errors)"
                if isinstance(v["errors"], int) else "FAILED"))
            for v in sorted(cur, key=lambda v: -v["rank"]))

    def is_green(self, epoch: int) -> bool:
        return bool(self.green_job) and self.green_epoch == epoch

    def observe(self, tool: str, key, scope: str, rank: int, result: dict,
                epoch: int, *, on_green: Callable[[str], None]) -> list[tuple[str, str]]:
        """Record one lint/sim result. Returns (guard, text) teachings.

        `on_green(job)` is called when every current check passes — the
        loop snapshots the tree there."""
        st = result.get("status")
        passed = st == "passed" or result.get("sim_status") == "pass"
        failed = st in ("failed", "error")
        if not (passed or failed):
            return []
        out: list[tuple[str, str]] = []
        jid = str(result.get("job") or "")
        prev = self.checks.get(key)
        if prev and prev["epoch"] == epoch and (key, epoch, "redo") not in self.scope_taught:
            # same check, same tree: the job cache made it cheap, but it
            # still cost a step and taught nothing — say so once
            self.scope_taught.add((key, epoch, "redo"))
            out.append(("scope_redo",
                        teachings.scope_redo(tool, scope, prev["job"], prev["passed"])))
        self.checks[key] = {"job": jid, "passed": passed, "epoch": epoch,
                            "rank": rank, "scope": scope, "tool": tool,
                            "errors": result.get("errors")}
        cur = self.current(epoch)
        if passed:
            if all(v["passed"] for v in cur):
                # green means EVERY current check passes; checkpoint under
                # the strongest of them
                strongest = max(cur, key=lambda v: v["rank"])
                if strongest["job"]:
                    on_green(strongest["job"])
                    self.green_job = strongest["job"]
                self.green_epoch = epoch
                self.fails_since_green = 0
                self.checkpoint_taught = False
        else:
            self.fails_since_green += 1
            # the tree is now KNOWN not-green: stop calling it green — but
            # keep the weaker pass on record, it is still a fact about this tree
            self.green_epoch = -1
            weaker = [v for v in cur if v["passed"] and v["rank"] < rank]
            if weaker and (key, epoch, "gap") not in self.scope_taught:
                self.scope_taught.add((key, epoch, "gap"))
                w = max(weaker, key=lambda v: v["rank"])
                out.append(("scope_gap", teachings.scope_gap(
                    w["tool"], w["scope"], w["job"], tool, scope, jid,
                    result.get("errors"))))
        return out
