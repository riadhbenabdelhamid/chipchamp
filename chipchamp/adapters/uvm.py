"""UVM report parsing + library location (SPEC §8.4 / §9-B DV methodology).

Simulator-neutral: the UVM report format comes from the library's own report
server, so the same parser reads Verilator, Questa (``# ``-prefixed transcript),
VCS and Xcelium logs. The one non-negotiable rule it enforces: a UVM run with
``UVM_ERROR``/``UVM_FATAL`` counts > 0 FAILED, even when the process exited 0 —
simulators happily ``$finish`` with a zero exit code after a failing test.

``uvm_home()`` finds the IEEE 1800.2 library (Accellera uvm-core) for the live
Verilator path; commercial simulators ship their own.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .base import NormalizedDiagnostic, ToolResult

# UVM_ERROR verif/scoreboard.sv(88) @ 105000: uvm_test_top.env.sb [SB] exp d3 got 7f
# UVM_FATAL @ 9200000: reporter [PH_TIMEOUT] Explicit timeout of 9200000 hit...
# (Questa transcripts prefix every line with "# ".)
_MSG = re.compile(
    r"^[#\s]*UVM_(?P<sev>INFO|WARNING|ERROR|FATAL)\s+"
    r"(?:(?P<file>[^\s(]+)\((?P<line>\d+)\)\s+)?"
    r"@\s*(?P<time>[\d.]+)\s*(?P<unit>[a-z]{0,2})\s*:\s*"
    r"(?P<ctx>\S+)\s*\[(?P<id>[^\]]+)\]\s*(?P<msg>.*)$")

# report-server summary:  UVM_ERROR :    2
_SUMMARY_COUNT = re.compile(
    r"^[#\s]*UVM_(?P<sev>INFO|WARNING|ERROR|FATAL)\s*:\s*(?P<n>\d+)\s*$")

_RUNNING_TEST = re.compile(r"\[RNTST\]\s*Running test\s+(?P<test>[\w:]+)")
_SEED = re.compile(
    r"(?:random seed|sv_seed|ntb_random_seed|svseed|sverilog seed|\+seed)"
    r"\s*[=: ]\s*(\d+)", re.IGNORECASE)


@dataclass
class UvmReport:
    is_uvm: bool = False
    test: str = ""
    seed: int | None = None
    counts: dict[str, int] = field(default_factory=dict)  # severity -> n
    passed: bool = True
    summary_seen: bool = False  # report-server summary block found (clean end)
    diagnostics: list[NormalizedDiagnostic] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.counts.get("ERROR", 0)

    @property
    def fatals(self) -> int:
        return self.counts.get("FATAL", 0)

    def verdict(self) -> str:
        if not self.is_uvm:
            return "not-uvm"
        if self.errors or self.fatals:
            return "fail"
        if not self.summary_seen:
            return "fail"  # no report summary => sim died before end_of_test
        return "pass"


def parse_uvm_log(text: str) -> UvmReport:
    """Parse a simulator log for UVM report-server output. ``is_uvm`` stays
    False when the log shows no UVM traffic (plain-TB logs pass through)."""
    rep = UvmReport()
    msg_counts: dict[str, int] = {}
    in_summary = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if "--- UVM Report Summary ---" in line:
            rep.is_uvm = True
            in_summary = True
            rep.summary_seen = True
            continue
        if in_summary:
            sm = _SUMMARY_COUNT.match(line)
            if sm:
                rep.counts[sm["sev"]] = int(sm["n"])
                continue
        m = _MSG.match(line)
        if not m:
            continue
        rep.is_uvm = True
        sev = m["sev"]
        msg_counts[sev] = msg_counts.get(sev, 0) + 1
        rt = _RUNNING_TEST.search(line)
        if rt:
            rep.test = rt["test"]
        if sev in ("ERROR", "FATAL"):
            rep.diagnostics.append(NormalizedDiagnostic(
                tool="uvm", severity="error", category="sim",
                code=f"UVM_{sev}:{m['id']}",
                file=m["file"] or "", line=int(m["line"] or 0),
                message=f"@{m['time']}{m['unit']} {m['ctx']} [{m['id']}] "
                        f"{(m['msg'] or '').strip()}"))
    # message-line tallies fill in when the summary block never printed
    for sev, n in msg_counts.items():
        rep.counts.setdefault(sev, n)
    sm = _SEED.search(text)
    if sm:
        rep.seed = int(sm.group(1))
    rep.passed = rep.verdict() == "pass"
    return rep


def enrich_sim_result(result: ToolResult, log: str) -> ToolResult:
    """Fold a UVM verdict into a sim ToolResult (call from sim adapters after
    the marker-based status). Downgrades pass→fail when the report server
    counted errors or the summary never printed; never upgrades a failure."""
    rep = parse_uvm_log(log)
    if not rep.is_uvm:
        return result
    result.metrics.update({
        "uvm": True, "uvm_test": rep.test,
        "uvm_errors": rep.errors, "uvm_fatals": rep.fatals,
        "uvm_warnings": rep.counts.get("WARNING", 0)})
    if rep.seed is not None and not result.metrics.get("seed"):
        result.metrics["seed"] = rep.seed
    result.diagnostics.extend(rep.diagnostics)
    if result.status == "pass" and not rep.passed:
        result.status = "fail"
        result.ok = False
        why = (f"UVM_ERROR={rep.errors} UVM_FATAL={rep.fatals}"
               if (rep.errors or rep.fatals)
               else "no UVM report summary (simulation ended prematurely)")
        result.summary = f"UVM test {rep.test or '?'} FAILED: {why}"
    elif result.status == "pass":
        result.summary = (f"UVM test {rep.test or '?'} passed "
                          f"({rep.counts.get('WARNING', 0)} warnings)")
    return result


def uvm_home() -> str | None:
    """IEEE 1800.2 library location: $UVM_HOME, else the platform cache
    (``~/.cache/chipchamp/uvm-core``, cloned from accellera-official)."""
    for cand in (os.environ.get("UVM_HOME", ""),
                 os.path.expanduser("~/.cache/chipchamp/uvm-core")):
        if cand and os.path.exists(os.path.join(cand, "src", "uvm_pkg.sv")):
            return cand
    return None
