"""Verible adapter — fast syntax-level lint + formatting (SPEC §8.4 M0)."""
from __future__ import annotations

import os

from .base import Adapter, CapabilityManifest, Plan, Step, StepResult, ToolResult
from .diagnostics import parse_verible


class VeribleAdapter(Adapter):
    name = "verible"
    binary = "verible-verilog-lint"
    cost = "cheap"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["lint", "format"], languages=["sv"],
            standards=["IEEE-1800-2017"], known_gaps=["no elaboration/CDC"])

    def lint(self, files: list[str], workdir: str, ruleset: str | None = None,
             waiver_file: str | None = None) -> Plan:
        argv = [self.binary]
        if ruleset:
            argv += [f"--rules_config={ruleset}"]
        if waiver_file and os.path.exists(waiver_file):
            argv += [f"--waiver_files={waiver_file}"]
        argv += list(files)
        return Plan(kind="lint", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)])

    def format(self, files: list[str], workdir: str) -> Plan:
        argv = ["verible-verilog-format", "--inplace", *files]
        return Plan(kind="format", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)])

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        if plan.kind == "format":
            ok = all(r.rc == 0 for r in results)
            return ToolResult(ok=ok, kind="format", adapter=self.name,
                              summary="formatted" if ok else "format failed")
        r = results[0]
        diags = parse_verible(r.stdout + "\n" + r.stderr)
        return ToolResult(
            ok=(len(diags) == 0), kind="lint", adapter=self.name,
            diagnostics=diags, metrics={"violations": len(diags)},
            summary=f"{len(diags)} style violation(s)")
