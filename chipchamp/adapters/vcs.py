"""Synopsys VCS adapter (SPEC §8.4 M2 tier).

Two-step ``vcs`` (compile+elab) then ``simv`` run, requesting VCD via the
testbench's dump hooks. Normalizes VCS's block-style diagnostics::

    Error-[ICPD] Illegal combination of ports
    rtl/dma.sv, 214
      message continues...

Fixture-validated; live validation is an M2 design-partner activity.
"""
from __future__ import annotations

import os
import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)
from .icarus import _status_from_log

_HEAD = re.compile(r"^(?P<sev>Error|Warning|Lint)-\[(?P<code>[\w-]+)\]\s*(?P<msg>.*)$")
_LOC = re.compile(r"^\s*\"?(?P<file>[\w./-]+)\"?,\s*(?P<line>\d+)\s*$")


def parse_vcs(text: str) -> list[NormalizedDiagnostic]:
    out: list[NormalizedDiagnostic] = []
    cur: NormalizedDiagnostic | None = None
    for ln in text.splitlines():
        m = _HEAD.match(ln.strip())
        if m:
            if cur:
                out.append(cur)
            sev = "error" if m["sev"] == "Error" else "warning"
            cur = NormalizedDiagnostic(tool="vcs", severity=sev, category="sim",
                                       code=m["code"], message=m["msg"].strip())
            continue
        if cur is not None:
            loc = _LOC.match(ln)
            if loc and not cur.file:
                cur.file = loc["file"]
                cur.line = int(loc["line"])
            elif ln.strip() and not cur.message:
                cur.message = ln.strip()
            elif not ln.strip():
                out.append(cur)
                cur = None
    if cur:
        out.append(cur)
    return out


class VcsAdapter(Adapter):
    name = "vcs"
    binary = "vcs"
    cost = "licensed"
    version_flags = ("-ID",)

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["sim", "coverage"], languages=["sv"],
            standards=["IEEE-1800-2023"], coverage_formats=["vdb"],
            wave_formats=["vcd", "fsdb"], xprop=True,
            license_features=["VCSRuntime_Net"],
            known_gaps=["normalizer fixture-validated; live validation pending (M2)",
                        "fsdb needs Verdi kit"])

    def sim(self, files: list[str], tb_top: str, workdir: str,
            seed: int | None = None, plusargs: list[str] | None = None,
            waves: bool = True, incdirs: list[str] | None = None,
            defines: dict | None = None, uvm: bool = False) -> Plan:
        simv = os.path.abspath(os.path.join(workdir, f"simv_{tb_top}"))
        vcd = os.path.abspath(os.path.join(workdir, f"{tb_top}.vcs.vcd"))
        compile_argv = ["vcs", "-full64", "-sverilog", "-timescale=1ns/1ps",
                        "-top", tb_top, "-o", simv]
        if uvm:
            compile_argv += ["-ntb_opts", "uvm"]  # VCS ships its own UVM
        for d in incdirs or []:
            compile_argv.append(f"+incdir+{d}")
        for k, v in (defines or {}).items():
            compile_argv.append(f"+define+{k}={v}")
        if waves:
            from ..brand import markers
            compile_argv += [f"+define+{mk}" for mk in markers("WAVES")]
        compile_argv += list(files)
        run_argv = [simv, "+vcs+lic+wait"]
        if seed is not None:
            run_argv.append(f"+ntb_random_seed={seed}")
        run_argv.append(f"+dumpfile={vcd}")
        run_argv += [f"+{p.lstrip('+')}" for p in (plusargs or [])]
        return Plan(kind="sim", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=compile_argv, cwd=workdir),
                           Step(argv=run_argv, cwd=workdir, allow_fail=True)],
                    artifacts={"waves": vcd} if waves else {},
                    license_features=["VCSRuntime_Net"],
                    meta={"tb_top": tb_top, "seed": seed})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        diags = parse_vcs("\n".join(r.stdout + "\n" + r.stderr for r in results))
        if results[0].rc != 0:
            return ToolResult(ok=False, kind="sim", adapter=self.name,
                              status="error", diagnostics=diags,
                              summary="vcs compile failed")
        run = results[1] if len(results) > 1 else None
        if run is None:
            return ToolResult(ok=False, kind="sim", adapter=self.name,
                              status="error", summary="simv did not run")
        log = run.stdout + run.stderr
        status, msg = _status_from_log(log, run)
        artifacts = {k: v for k, v in plan.artifacts.items() if os.path.exists(v)}
        result = ToolResult(ok=(status == "pass"), kind="sim", adapter=self.name,
                            status=status, diagnostics=diags, artifacts=artifacts,
                            metrics={"seed": plan.meta.get("seed")}, summary=msg)
        from .uvm import enrich_sim_result
        return enrich_sim_result(result, log)


class PrimeTimeAdapter(Adapter):
    """PrimeTime STA (report parser shares the OpenSTA normalizer — the
    Startpoint/Endpoint/slack grammar is common to both)."""

    name = "primetime"
    binary = "pt_shell"
    cost = "licensed"
    version_flags = ("-version",)

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["sta"], languages=["verilog-netlist"],
            license_features=["PrimeTime"],
            known_gaps=["report parser shared w/ OpenSTA (fixture-validated); "
                        "live validation pending (M2)"])

    def sta(self, netlist: str, top: str, workdir: str, *, liberty: str,
            sdc: str, n_paths: int = 5) -> Plan:
        os.makedirs(workdir, exist_ok=True)
        tcl = [f"set link_library [list {liberty}]",
               f"read_verilog {netlist}", f"link_design {top}",
               f"read_sdc {sdc}",
               f"report_timing -max_paths {n_paths} -path_type full",
               "report_global_timing", "exit"]
        script = os.path.join(workdir, f"pt_{top}.tcl")
        with open(script, "w") as fh:
            fh.write("\n".join(tcl) + "\n")
        return Plan(kind="sta", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=["pt_shell", "-f", script], cwd=workdir,
                                allow_fail=True)],
                    license_features=["PrimeTime"], meta={"top": top})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        from .opensta import parse_path_report
        r = results[0]
        text = r.stdout + "\n" + r.stderr
        paths = parse_path_report(text)
        violations = sum(1 for p in paths if p["violated"])
        wns = min((p["slack"] for p in paths), default=None)
        return ToolResult(
            ok=(r.rc == 0 and violations == 0), kind="sta", adapter=self.name,
            status="clean" if violations == 0 else "violated",
            metrics={"wns": wns, "violations": violations, "paths": paths,
                     "mode": "liberty-timed"},
            summary=f"WNS={wns} violations={violations}")
