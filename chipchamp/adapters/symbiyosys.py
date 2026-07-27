"""SymbiYosys (sby) adapter — bounded/unbounded formal property checking.

Backs the ``formal.*`` tools and the interface-property step of playbook P3.
Generates an sby project for a module carrying SVA and parses PASS/FAIL/UNKNOWN,
materializing counterexample traces for the waveform service (`formal.cex_to_wave`).
"""
from __future__ import annotations

import os
import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)


class SymbiYosysAdapter(Adapter):
    name = "symbiyosys"
    binary = "sby"
    cost = "metered"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["formal"], languages=["sv"],
            wave_formats=["vcd"],
            known_gaps=["SVA synth subset", "assumptions must be synthesizable"])

    def formal(self, files: list[str], top: str, workdir: str,
               mode: str = "prove", depth: int = 20) -> Plan:
        os.makedirs(workdir, exist_ok=True)
        # -formal enables SVA/immediate assert/assume/cover in the open frontend
        reads = "\n".join(f"read -formal {os.path.basename(f)}" for f in files)
        cfg = f"""[options]
mode {mode}
depth {depth}

[engines]
smtbmc

[script]
{reads}
prep -top {top}

[files]
""" + "\n".join(os.path.abspath(f) for f in files) + "\n"
        cfg_path = os.path.join(workdir, f"formal_{top}.sby")
        with open(cfg_path, "w") as fh:
            fh.write(cfg)
        argv = ["sby", "-f", cfg_path]
        return Plan(kind="formal", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)],
                    meta={"top": top, "config": cfg_path, "depth": depth})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0]
        text = r.stdout + "\n" + r.stderr
        status = "unknown"
        if re.search(r"DONE \(PASS", text) or re.search(r"\bPASS\b", text):
            status = "pass"
        if re.search(r"DONE \(FAIL", text) or re.search(r"\bFAIL\b", text):
            status = "fail"
        diags = []
        cex = None
        for m in re.finditer(r"Assert failed in \S+: (\S+)", text):
            diags.append(NormalizedDiagnostic(
                tool="sby", severity="error", category="formal", code="ASSERT-FAIL",
                message=f"assertion {m.group(1)} failed (counterexample found)"))
        mcex = re.search(r"writing.*?(\S+\.vcd)", text)
        if mcex:
            cex = mcex.group(1)
        artifacts = {"cex_waves": cex} if cex else {}
        return ToolResult(
            ok=(status == "pass"), kind="formal", adapter=self.name, status=status,
            diagnostics=diags, artifacts=artifacts,
            metrics={"depth": plan.meta.get("depth")},
            summary=f"formal {status}")
