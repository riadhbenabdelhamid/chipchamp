"""The compile-then-run simulation verdict, written once.

Four simulator adapters each carried the same twenty lines: did the compile
step fail, did the run step exist, join its stdout and stderr, derive a
status from the log's markers, filter artifacts, build the ToolResult, fold
in the UVM report. They had already drifted — two joined stdout and stderr
with no newline between them, so a UVM tally at the start of stderr glued
onto stdout's last line, escaped the line-anchored tally filter, and read
as a FATAL event; one kept missing artifact paths that the others dropped;
only one collected runtime diagnostics.

Each adapter keeps what is really its own — the diagnostic parser for its
tool — and calls parse_compile_run_sim() for the rest.
"""
from __future__ import annotations

import os
import re

from ..brand import markers
from .base import NormalizedDiagnostic, Plan, StepResult, ToolResult


def status_from_log(log: str, run_res: StepResult) -> tuple[str, str]:
    """(status, message) for a simulation run from its log and step result."""
    if run_res.timed_out:
        return "timeout", "simulation timed out"
    # UVM report-summary tallies ("UVM_FATAL :    0") are counts, not events —
    # drop them before keyword checks; uvm.enrich_sim_result folds in the real
    # UVM verdict (and never upgrades a failure).
    log = re.sub(r"(?m)^[#\s]*UVM_(?:INFO|WARNING|ERROR|FATAL)\s*:\s*\d+\s*$", "", log)
    fail_hit = next((mk for mk in markers("FAIL") if mk in log), None)
    if fail_hit:
        m = re.search(rf"{fail_hit}:?\s*(.*)", log)
        return "fail", (m.group(1).strip() if m else "testbench reported failure")
    if re.search(r"\$fatal|FATAL|UVM_FATAL", log):
        return "fail", "fatal error in simulation"
    if re.search(r"UVM_ERROR\s*[:@]", log) or re.search(r"\bError\b.*assert", log):
        return "fail", "errors during simulation"
    if any(mk in log for mk in markers("PASS")):
        return "pass", "testbench reported pass"
    if run_res.rc == 0:
        return "pass", "completed (no failure markers)"
    return "fail", f"non-zero exit ({run_res.rc})"


def runtime_diags(log: str, tool: str) -> list[NormalizedDiagnostic]:
    """`file:line: message` lines that look like assertion/mismatch reports."""
    out = []
    for m in re.finditer(r"(?P<file>[\w./-]+):(?P<line>\d+):\s*(?P<msg>.*(?:assert|ERROR|mismatch|FAIL).*)",
                         log, re.I):
        out.append(NormalizedDiagnostic(
            tool=tool, severity="error", category="sim", code="SIM",
            file=m["file"], line=int(m["line"]), message=m["msg"].strip()))
    return out


def run_log(res: StepResult) -> str:
    """stdout and stderr as one log, with a newline between them — always."""
    return (res.stdout or "") + "\n" + (res.stderr or "")


def parse_compile_run_sim(adapter: str, plan: Plan, results: list[StepResult],
                          diags: list[NormalizedDiagnostic], *,
                          compile_failed: str = "compile failed",
                          did_not_run: str = "did not run",
                          runtime_tool: str | None = None) -> ToolResult:
    """Verdict for a two-step plan (compile, then run).

    `diags` are the adapter's own parsed diagnostics. `runtime_tool`, when
    given, also harvests file:line assertion reports from the run log under
    that tool name and reports their count as `assertion_failures`.
    """
    if not results:
        return ToolResult(ok=False, kind="sim", adapter=adapter, status="error",
                          summary="no steps ran")
    diags = list(diags)
    if results[0].rc != 0:
        return ToolResult(ok=False, kind="sim", adapter=adapter, status="error",
                          diagnostics=diags, summary=compile_failed,
                          metrics={"phase": "compile"})
    run = results[1] if len(results) > 1 else None
    if run is None:
        return ToolResult(ok=False, kind="sim", adapter=adapter, status="error",
                          diagnostics=diags, summary=did_not_run)
    log = run_log(run)
    metrics: dict = {"seed": plan.meta.get("seed")}
    if runtime_tool:
        extra = runtime_diags(log, runtime_tool)
        diags += extra
        metrics["assertion_failures"] = sum(1 for d in diags if d.category == "sim")
    status, msg = status_from_log(log, run)
    artifacts = {k: v for k, v in plan.artifacts.items() if v and os.path.exists(v)}
    result = ToolResult(ok=(status == "pass"), kind="sim", adapter=adapter,
                        status=status, diagnostics=diags, artifacts=artifacts,
                        metrics=metrics, summary=msg)
    from .uvm import enrich_sim_result
    return enrich_sim_result(result, log)
