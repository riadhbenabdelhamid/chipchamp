"""GHDL adapter — VHDL analysis/elaboration (SPEC §8.4, open Q#4).

VHDL is parse/elaborate-level in this build (SV-first, per the roadmap). This
adapter runs ``ghdl -a`` (analyze) and ``ghdl -e`` (elaborate) so mixed-language
projects at least get V0 (parse+elaborate) feedback for their VHDL units.
"""
from __future__ import annotations

import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)


class GhdlAdapter(Adapter):
    name = "ghdl"
    binary = "ghdl"
    cost = "cheap"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["elab"], languages=["vhdl"],
            standards=["IEEE-1076-2008"],
            known_gaps=["VHDL parse/elaborate only in this build; no sim/coverage"])

    def elaborate(self, files: list[str], top: str, workdir: str,
                  std: str = "08") -> Plan:
        analyze = ["ghdl", "-a", f"--std={std}", *files]
        elab = ["ghdl", "-e", f"--std={std}", top]
        return Plan(kind="elab", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=analyze, cwd=workdir, allow_fail=True),
                           Step(argv=elab, cwd=workdir, allow_fail=True)],
                    meta={"top": top})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        diags = []
        for r in results:
            for ln in (r.stdout + "\n" + r.stderr).splitlines():
                m = re.match(r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.*)$", ln)
                if m:
                    sev = "error" if "error" in m["msg"].lower() or ":" in ln[:0] else "warning"
                    sev = "error" if r.rc != 0 else "warning"
                    diags.append(NormalizedDiagnostic(
                        tool="ghdl", severity=sev, category="elab", code="VHDL",
                        file=m["file"], line=int(m["line"]), col=int(m["col"]),
                        message=m["msg"]))
        ok = all(r.rc == 0 for r in results)
        return ToolResult(ok=ok, kind="elab", adapter=self.name, diagnostics=diags,
                          summary="vhdl elaborate ok" if ok else "vhdl elaborate failed")
