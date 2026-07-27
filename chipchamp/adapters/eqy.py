"""eqy adapter — logic equivalence checking (SPEC §8.4, `lec.run`).

LEC is the gate that lets a "no functional change" refactor be trusted (SPEC
§11.1, playbook P7). This adapter generates an eqy project that proves the
golden and revised modules equivalent and parses the verdict; an inconclusive
result downgrades the task to a functional change rather than passing silently.
"""
from __future__ import annotations

import os
import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)


class EqyAdapter(Adapter):
    name = "eqy"
    binary = "eqy"
    cost = "metered"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["lec"], languages=["sv"],
            known_gaps=["sequential depth-bounded", "SV synth subset only"])

    def lec(self, gold_files: list[str], revised_files: list[str], top: str,
            workdir: str, depth: int = 10) -> Plan:
        os.makedirs(workdir, exist_ok=True)
        gold_reads = "\n".join(f"read -sv {os.path.abspath(f)}" for f in gold_files)
        gate_reads = "\n".join(f"read -sv {os.path.abspath(f)}" for f in revised_files)
        # NOTE: depth is a per-strategy option; a top-level [options] depth is
        # rejected by eqy ("unknown option").
        cfg = f"""[gold]
{gold_reads}
prep -top {top}

[gate]
{gate_reads}
prep -top {top}

[strategy sby]
use sby
depth {depth}

[strategy pdr]
use sby
depth {depth}
engine abc pdr
"""
        cfg_path = os.path.join(workdir, f"lec_{top}.eqy")
        with open(cfg_path, "w") as fh:
            fh.write(cfg)
        # eqy writes into a dir named after the config; force fresh.
        argv = ["eqy", "-f", cfg_path]
        return Plan(kind="lec", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)],
                    meta={"top": top, "config": cfg_path})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0]
        text = r.stdout + "\n" + r.stderr
        verdict = "inconclusive"
        reason = None
        if re.search(r"Successfully proved designs equivalent|Equivalence successfully proven",
                     text, re.I):
            verdict = "equivalent"
        elif re.search(r"proved.*equivalent", text, re.I) and r.rc == 0:
            verdict = "equivalent"
        elif re.search(r"Failed to prove|not equivalent|FAIL", text, re.I):
            verdict = "non-equivalent"
        elif re.search(r"UNKNOWN|inconclusive|timeout|reached depth", text, re.I):
            verdict = "inconclusive"
            reason = "bounded proof reached depth without full equivalence"
        elif r.rc == 0:
            verdict = "equivalent"
        diags = []
        if verdict == "non-equivalent":
            for m in re.finditer(r"(?:partition|point)\s+(\S+)", text):
                diags.append(NormalizedDiagnostic(
                    tool="eqy", severity="error", category="lec", code="LEC-DIFF",
                    message=f"non-equivalent partition {m.group(1)}"))
        return ToolResult(
            ok=(verdict == "equivalent"), kind="lec", adapter=self.name,
            status=verdict, diagnostics=diags,
            metrics={"verdict": verdict, "inconclusive_reason": reason},
            summary=f"LEC verdict: {verdict}")
