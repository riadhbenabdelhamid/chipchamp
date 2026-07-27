"""Questa/ModelSim adapter (SPEC §8.4 M2 tier).

Drives the customer's licensed ``vlog``/``vsim`` installation; Chipchamp ships
glue only. The diagnostic normalizer is validated against fixture logs in the
test suite; live-tool validation is an M2 design-partner activity — the adapter
degrades to unavailable when the binaries are absent.
"""
from __future__ import annotations

import os
import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)
from .icarus import _status_from_log

# ** Error: rtl/dma.sv(214): (vlog-2892) message...
# ** Warning: (vsim-3015) tb.sv(88): message...
_QUESTA_DIAG = re.compile(
    r"\*\*\s*(?P<sev>Error|Warning|Fatal)"
    r"(?:\s*\((?P<code1>[\w-]+)\))?:\s*"
    r"(?:\((?P<code2>[\w-]+)\)\s*)?"
    r"(?:(?P<file>[^\s(]+)\((?P<line>\d+)\):\s*)?"
    r"(?:\((?P<code3>[\w-]+)\)\s*)?"
    r"(?P<msg>.*)$")


def parse_questa(text: str) -> list[NormalizedDiagnostic]:
    out = []
    for ln in text.splitlines():
        m = _QUESTA_DIAG.match(ln.strip())
        if not m:
            continue
        sev = "error" if m["sev"] in ("Error", "Fatal") else "warning"
        code = m["code1"] or m["code2"] or m["code3"] or "QUESTA"
        out.append(NormalizedDiagnostic(
            tool="questa", severity=sev, category="sim",
            code=code, file=m["file"] or "", line=int(m["line"] or 0),
            message=(m["msg"] or "").strip()))
    return out


class QuestaAdapter(Adapter):
    name = "questa"
    binary = "vsim"
    cost = "licensed"
    version_flags = ("-version",)

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["sim", "coverage"], languages=["sv", "vhdl"],
            standards=["IEEE-1800-2017", "IEEE-1076-2008"],
            coverage_formats=["ucdb"], wave_formats=["wlf", "vcd"],
            xprop=True, license_features=["questa_sim"],
            known_gaps=["normalizer fixture-validated; live validation pending (M2)"])

    def sim(self, files: list[str], tb_top: str, workdir: str,
            seed: int | None = None, plusargs: list[str] | None = None,
            waves: bool = True, incdirs: list[str] | None = None,
            defines: dict | None = None, uvm: bool = False) -> Plan:
        lib = os.path.abspath(os.path.join(workdir, "work"))
        vcd = os.path.abspath(os.path.join(workdir, f"{tb_top}.questa.vcd"))
        vlog = ["vlog", "-sv", "-work", lib]
        files = list(files)
        if uvm:
            # explicit uvm-core sources keep this vendor-neutral; customer
            # installs may prefer the precompiled lib (vlog -L mtiUvm)
            from .uvm import uvm_home
            home = uvm_home()
            if home:
                vlog += [f"+incdir+{os.path.join(home, 'src')}",
                         "+define+UVM_NO_DPI", "+define+UVM_REGEX_NO_DPI"]
                files = [os.path.join(home, "src", "uvm_pkg.sv")] + files
        for d in incdirs or []:
            vlog.append(f"+incdir+{d}")
        for k, v in (defines or {}).items():
            vlog.append(f"+define+{k}={v}")
        if waves:
            from ..brand import markers
            vlog += [f"+define+{mk}" for mk in markers("WAVES")]
        vlog += list(files)
        do = f"vcd file {vcd}; vcd add -r /*; run -all; quit -f" if waves \
            else "run -all; quit -f"
        vsim = ["vsim", "-c", "-lib", lib, tb_top, "-do", do]
        if seed is not None:
            vsim += ["-sv_seed", str(seed)]
        for p in plusargs or []:
            vsim.append(f"+{p.lstrip('+')}")
        return Plan(kind="sim", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=vlog, cwd=workdir),
                           Step(argv=vsim, cwd=workdir, allow_fail=True)],
                    artifacts={"waves": vcd} if waves else {},
                    license_features=["questa_sim"],
                    meta={"tb_top": tb_top, "seed": seed})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        diags = parse_questa("\n".join(r.stdout + "\n" + r.stderr for r in results))
        if results[0].rc != 0:
            return ToolResult(ok=False, kind="sim", adapter=self.name,
                              status="error", diagnostics=diags,
                              summary="vlog compile failed")
        run = results[1] if len(results) > 1 else None
        if run is None:
            return ToolResult(ok=False, kind="sim", adapter=self.name,
                              status="error", summary="vsim did not run")
        log = run.stdout + run.stderr
        status, msg = _status_from_log(log, run)
        artifacts = {k: v for k, v in plan.artifacts.items() if os.path.exists(v)}
        result = ToolResult(ok=(status == "pass"), kind="sim", adapter=self.name,
                            status=status, diagnostics=diags, artifacts=artifacts,
                            metrics={"seed": plan.meta.get("seed")}, summary=msg)
        from .uvm import enrich_sim_result
        return enrich_sim_result(result, log)
