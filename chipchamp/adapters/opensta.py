"""OpenSTA adapter (SPEC §8.4/§9-H): static timing analysis via `sta`.

Real STA needs a liberty library + SDC constraints; the adapter builds the TCL,
runs `report_checks`, and normalizes the classic OpenSTA/PrimeTime-style path
report (Startpoint/Endpoint/slack). The report parser is fixture-tested so the
adapter is ready the day a customer machine has `sta` on PATH; on machines
without it (like this one) the platform falls back to the Yosys structural-depth
proxy (see YosysAdapter.timing_snapshot).
"""
from __future__ import annotations

import os
import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)


class OpenStaAdapter(Adapter):
    name = "opensta"
    binary = "sta"
    cost = "metered"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["sta"], languages=["verilog-netlist"],
            known_gaps=["needs liberty + SDC; gate-level netlist input"])

    def sta(self, netlist: str, top: str, workdir: str, *,
            liberty: str | None = None, sdc: str | None = None,
            clock_port: str = "clk", period_ns: float = 10.0,
            n_paths: int = 5) -> Plan:
        os.makedirs(workdir, exist_ok=True)
        tcl = []
        if liberty:
            tcl.append(f"read_liberty {liberty}")
        tcl.append(f"read_verilog {netlist}")
        tcl.append(f"link_design {top}")
        if sdc:
            tcl.append(f"read_sdc {sdc}")
        else:
            tcl.append(f"create_clock -name clk -period {period_ns} [get_ports {clock_port}]")
        tcl.append(f"report_checks -path_delay max -group_count {n_paths} -format full")
        tcl.append("report_wns")
        tcl.append("report_tns")
        tcl.append("exit")
        script = os.path.join(workdir, f"sta_{top}.tcl")
        with open(script, "w") as fh:
            fh.write("\n".join(tcl) + "\n")
        return Plan(kind="sta", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=["sta", "-no_splash", script],
                                cwd=workdir, allow_fail=True)],
                    meta={"top": top, "script": script})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0]
        text = r.stdout + "\n" + r.stderr
        paths = parse_path_report(text)
        wns = _first_float(r"wns\s+(-?\d+\.?\d*)", text)
        tns = _first_float(r"tns\s+(-?\d+\.?\d*)", text)
        if wns is None and paths:
            wns = min(p["slack"] for p in paths)
        violations = sum(1 for p in paths if p["violated"])
        diags = [NormalizedDiagnostic(
            tool=self.name, severity="warning", category="sta", code="TIMING-VIOLATION",
            message=f"{p['startpoint']} -> {p['endpoint']} slack {p['slack']}")
            for p in paths if p["violated"]]
        ok = r.rc == 0
        return ToolResult(
            ok=ok and violations == 0, kind="sta", adapter=self.name,
            status="clean" if violations == 0 else "violated",
            diagnostics=diags,
            metrics={"wns": wns, "tns": tns, "violations": violations,
                     "paths": paths, "mode": "liberty-timed"},
            summary=f"WNS={wns} TNS={tns} violations={violations}")


def parse_path_report(text: str) -> list[dict]:
    """Parse OpenSTA/PrimeTime-style `report_checks -format full` output into
    normalized path records. Format is stable across both tools:

        Startpoint: u_a/q_reg (rising edge-triggered flip-flop clocked by clk)
        Endpoint: u_b/d_reg (...)
        ...
        -0.12   slack (VIOLATED)
    """
    paths: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"Startpoint:\s*(\S+)", s)
        if m:
            cur = {"startpoint": m.group(1), "endpoint": "", "slack": 0.0,
                   "violated": False}
            continue
        m = re.match(r"Endpoint:\s*(\S+)", s)
        if m and cur is not None:
            cur["endpoint"] = m.group(1)
            continue
        m = re.match(r"(-?\d+\.?\d*)\s+slack\s+\((VIOLATED|MET)\)", s)
        if m and cur is not None:
            cur["slack"] = float(m.group(1))
            cur["violated"] = m.group(2) == "VIOLATED"
            paths.append(cur)
            cur = None
    return paths


def _first_float(pattern: str, text: str):
    m = re.search(pattern, text, re.I)
    return float(m.group(1)) if m else None
