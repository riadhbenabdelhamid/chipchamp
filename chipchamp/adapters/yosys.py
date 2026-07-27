"""Yosys adapter — synthesis snapshot as an RTL feedback signal (SPEC §8.4, NG4).

Not a signoff synthesis flow: it gives the agent cheap, license-free structural
feedback — cell/area estimate, inferred-latch and combinational-loop warnings —
so RTL decisions (playbook P9 timing, latch hunts) have a real backend signal.
"""
from __future__ import annotations

import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)
from .diagnostics import parse_yosys


class YosysAdapter(Adapter):
    name = "yosys"
    binary = "yosys"
    cost = "cheap"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["synth", "elab"], languages=["sv"],
            standards=["IEEE-1800 (synth subset)"],
            known_gaps=["read_verilog SV subset", "estimate only, not signoff area"])

    @staticmethod
    def _read_prefix(incdirs, defines) -> str:
        """`-I<dir>`/`-D<macro>` flags for read_verilog. SYNTHESIS is defined
        by default so a reused library component's SVA assertions (guarded by
        `ifdef SYNTHESIS → NO_ASSERTS`) compile out — yosys's SVA support is
        partial and would otherwise choke on them."""
        parts = [f"-I{d}" for d in (incdirs or [])]
        defs = {"SYNTHESIS": 1, **(defines or {})}
        parts += [f"-D{k}={v}" for k, v in defs.items()]
        return " ".join(parts)

    @staticmethod
    def _order_for_yosys(files: list[str]) -> list[str]:
        """yosys's `read_verilog` is sequential and single-pass, so unlike
        Verilator it needs (1) header files (`.svh`/`.vh`) dropped — they are
        `include`d via the -I path, not compiled standalone — and (2) package
        definitions read BEFORE their users, or `pkg::fn` won't resolve."""
        src = [f for f in files if not f.endswith((".svh", ".vh"))]

        def is_pkg(f: str) -> bool:
            try:
                head = open(f, errors="replace").read(4000)
            except OSError:
                return False
            return re.search(r"^\s*package\s+\w+\s*;", head, re.M) is not None

        pkgs = [f for f in src if is_pkg(f)]
        return pkgs + [f for f in src if f not in pkgs]

    def synth(self, files: list[str], top: str, workdir: str,
              incdirs: list[str] | None = None,
              defines: dict | None = None) -> Plan:
        pre = self._read_prefix(incdirs, defines)
        files = self._order_for_yosys(files)
        reads = "; ".join(f"read_verilog -sv {pre} {f}" for f in files)
        script = (
            f"{reads}; "
            f"hierarchy -top {top}; proc; opt; memory -nomap; opt; "
            f"stat; "
            f"select -count t:$dlatch t:$_DLATCH_*; "
            f"check -noinit"
        )
        argv = ["yosys", "-p", script]
        return Plan(kind="synth", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)],
                    meta={"top": top})

    def timing_snapshot(self, files: list[str], top: str, workdir: str,
                        incdirs: list[str] | None = None,
                        defines: dict | None = None) -> Plan:
        """Structural timing proxy (SPEC §9-H fallback): longest topological
        path (`ltp`) after generic synthesis. No liberty timing — reports logic
        DEPTH, not delay. Honest signal for 'did my edit lengthen the path'."""
        pre = self._read_prefix(incdirs, defines)
        files = self._order_for_yosys(files)
        reads = "; ".join(f"read_verilog -sv {pre} {f}" for f in files)
        script = (f"{reads}; hierarchy -top {top}; proc; opt; memory -nomap; "
                  f"opt; techmap; opt; stat; ltp -noff")
        argv = ["yosys", "-p", script]
        return Plan(kind="sta", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)],
                    meta={"top": top, "mode": "structural-depth"})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        if plan.kind == "sta":
            return self._parse_timing(plan, results)
        r = results[0]
        text = r.stdout + "\n" + r.stderr
        diags = parse_yosys(text)
        metrics = {}
        mcells = re.search(r"Number of cells:\s+(\d+)", text) or \
            re.search(r"^\s*(\d+)\s+cells\s*$", text, re.M)
        if mcells:
            metrics["cells"] = int(mcells.group(1))
        mwire = re.search(r"Number of wires:\s+(\d+)", text) or \
            re.search(r"^\s*(\d+)\s+wires\s*$", text, re.M)
        if mwire:
            metrics["wires"] = int(mwire.group(1))
        # `select -count` on latch types prints "N objects" (or "0 objects").
        latches = 0
        for m in re.finditer(r"(\d+) objects?\b", text):
            latches = int(m.group(1))
        metrics["latches"] = latches
        if latches:
            diags.append(NormalizedDiagnostic(
                tool="yosys", severity="warning", category="synth",
                code="INFERRED-LATCH",
                message=f"{latches} inferred latch cell(s) after synthesis "
                        f"(check for missing default/else assignments)"))
        ok = r.rc == 0 and not any(d.severity == "error" for d in diags)
        return ToolResult(
            ok=ok, kind="synth", adapter=self.name, diagnostics=diags,
            metrics=metrics,
            summary=f"cells={metrics.get('cells','?')} latches={latches}")

    def _parse_timing(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0]
        text = r.stdout + "\n" + r.stderr
        m = re.search(r"Longest topological path in (\S+) \(length=(\d+)\)", text)
        depth = int(m.group(2)) if m else None
        # the numbered path nodes following the header
        nodes = re.findall(r"^\s*\d+:\s*(\S+)", text, re.M)
        mcells = re.search(r"^\s*(\d+)\s+cells\s*$", text, re.M)
        ok = r.rc == 0 and depth is not None
        return ToolResult(
            ok=ok, kind="sta", adapter=self.name,
            status="proxy" if ok else "error",
            metrics={"depth": depth, "path": nodes[:12],
                     "cells": int(mcells.group(1)) if mcells else None,
                     "mode": "structural-depth",
                     "note": "logic-depth proxy (no liberty timing); "
                             "use OpenSTA/PrimeTime for signoff"},
            summary=f"longest topological path: {depth} levels"
                    if ok else "ltp failed")
