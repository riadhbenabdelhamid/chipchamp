"""Icarus Verilog adapter — the default inner-loop simulator (SPEC §8.4 M0).

Compiles a SystemVerilog testbench with ``iverilog -g2012`` and runs it under
``vvp``. Pass/fail is read from testbench markers (``CHIPCHAMP_PASS`` /
``CHIPCHAMP_FAIL: <msg>`` — markers from any earlier brand era are
still accepted, see brand.markers) and the process return code; a VCD is
produced by the testbench's ``$dumpfile``/``$dumpvars`` and handed to the
waveform service.
"""
from __future__ import annotations

import os
import re

from ..brand import marker, markers
from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)
from .diagnostics import parse_icarus

# canonical marker for anything that GENERATES a testbench; parsers accept
# the whole markers() family so pre-rename harnesses keep verifying
PASS_MARK = marker("PASS")
FAIL_MARK = marker("FAIL")


class IcarusAdapter(Adapter):
    name = "icarus"
    binary = "iverilog"
    cost = "cheap"
    version_flags = ("-V", "-v")

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["sim"], languages=["sv"],
            standards=["IEEE-1800-2012"], wave_formats=["vcd", "lxt2", "fst"],
            xprop=True,
            known_gaps=["partial SVA/UVM support", "no functional coverage DB"])

    def sim(self, files: list[str], tb_top: str, workdir: str,
            seed: int | None = None, plusargs: list[str] | None = None,
            waves: bool = True, incdirs: list[str] | None = None,
            defines: dict | None = None) -> Plan:
        # absolute so paths are unambiguous regardless of the step's cwd
        vvp_out = os.path.abspath(os.path.join(workdir, f"{tb_top}.vvp"))
        vcd = os.path.abspath(os.path.join(workdir, f"{tb_top}.vcd"))
        compile_argv = ["iverilog", "-g2012", "-grelative-include",
                        "-s", tb_top, "-o", vvp_out]
        for d in incdirs or []:
            compile_argv += ["-I", d]
        for k, v in (defines or {}).items():
            compile_argv.append(f"-D{k}={v}")
        if waves:
            compile_argv += [f"-D{mk}" for mk in markers("WAVES")]
        compile_argv += list(files)

        run_argv = ["vvp", vvp_out]
        if waves:
            run_argv += ["-fst"] if False else []  # keep default VCD via $dumpvars
        pargs = list(plusargs or [])
        if seed is not None:
            pargs.append(f"+seed={seed}")
        pargs.append(f"+dumpfile={vcd}")
        run_argv += pargs
        return Plan(
            kind="sim", adapter=self.name, workdir=workdir,
            steps=[Step(argv=compile_argv, cwd=workdir),
                   Step(argv=run_argv, cwd=workdir, allow_fail=True)],
            artifacts={"waves": vcd, "vvp": vvp_out},
            meta={"tb_top": tb_top, "seed": seed})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        diags: list[NormalizedDiagnostic] = []
        compile_res = results[0]
        diags += parse_icarus(compile_res.stdout + "\n" + compile_res.stderr)
        if compile_res.rc != 0:
            return ToolResult(
                ok=False, kind="sim", adapter=self.name, status="error",
                diagnostics=diags, summary="compile failed",
                metrics={"phase": "compile"})
        run_res = results[1] if len(results) > 1 else None
        if run_res is None:
            return ToolResult(ok=False, kind="sim", adapter=self.name,
                              status="error", summary="did not run")
        log = run_res.stdout + "\n" + run_res.stderr
        diags += _runtime_diags(log)
        status, msg = _status_from_log(log, run_res)
        artifacts = dict(plan.artifacts)
        if not os.path.exists(artifacts.get("waves", "")):
            artifacts.pop("waves", None)
        result = ToolResult(
            ok=(status == "pass"), kind="sim", adapter=self.name, status=status,
            diagnostics=diags, artifacts=artifacts,
            metrics={"seed": plan.meta.get("seed"),
                     "assertion_failures": sum(1 for d in diags if d.category == "sim")},
            summary=msg)
        from .uvm import enrich_sim_result
        return enrich_sim_result(result, log)


def _status_from_log(log: str, run_res: StepResult) -> tuple[str, str]:
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


def _runtime_diags(log: str) -> list[NormalizedDiagnostic]:
    out = []
    for m in re.finditer(r"(?P<file>[\w./-]+):(?P<line>\d+):\s*(?P<msg>.*(?:assert|ERROR|mismatch|FAIL).*)",
                         log, re.I):
        out.append(NormalizedDiagnostic(
            tool="iverilog", severity="error", category="sim", code="SIM",
            file=m["file"], line=int(m["line"]), message=m["msg"].strip()))
    return out
