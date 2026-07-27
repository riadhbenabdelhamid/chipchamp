"""Verilator adapter — elaboration-aware lint and code coverage (SPEC §8.4 M0).

Verilator is the inner-loop lint/coverage workhorse: fast, license-free, and
elaboration-accurate enough to catch width, latch and combinational-loop issues
that a syntax linter misses.
"""
from __future__ import annotations

import os
import re

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)
from .diagnostics import parse_verilator


class VerilatorAdapter(Adapter):
    name = "verilator"
    binary = "verilator"
    cost = "cheap"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["lint", "coverage", "sim", "elab"],
            languages=["sv"], standards=["IEEE-1800-2017", "IEEE-1800-2023"],
            coverage_formats=["verilator-dat"], wave_formats=["vcd", "fst"],
            xprop=False,
            known_gaps=["2-state by default (X-prop limited)",
                        "no timing controls in some UVM styles"])

    def lint(self, files: list[str], workdir: str, top: str | None = None,
             incdirs: list[str] | None = None, defines: dict | None = None,
             extra: list[str] | None = None) -> Plan:
        argv = [self.binary, "--lint-only", "-sv", "-Wall"]
        for d in incdirs or []:
            argv.append(f"+incdir+{d}")
        for k, v in (defines or {}).items():
            argv.append(f"+define+{k}={v}")
        if top:
            argv += ["--top-module", top]
        argv += list(extra or [])
        argv += list(files)
        return Plan(kind="lint", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)])

    def elaborate(self, files: list[str], workdir: str, top: str) -> Plan:
        plan = self.lint(files, workdir, top=top, extra=["-Wno-lint"])
        plan.kind = "elab"
        return plan

    def sim(self, files: list[str], tb_top: str, workdir: str,
            seed: int | None = None, plusargs: list[str] | None = None,
            waves: bool = True, incdirs: list[str] | None = None,
            defines: dict | None = None, uvm: bool = False) -> Plan:
        """Compile+run with ``--binary --timing``. ``uvm=True`` compiles the
        IEEE 1800.2 library (Accellera uvm-core via :func:`uvm.uvm_home`) in
        front of the testbench — the license-free UVM path."""
        objdir = os.path.abspath(os.path.join(workdir, f"obj_{tb_top}"))
        vcd = os.path.abspath(os.path.join(workdir, f"{tb_top}.verilator.vcd"))
        build = [self.binary, "--binary", "--timing", "-sv", "-Wno-fatal",
                 "-j", "0",  # parallel verilate+make (UVM builds are large)
                 "--top-module", tb_top, "--Mdir", objdir, "-o", "Vsim"]
        if waves:
            from ..brand import markers
            build += ["--trace", *(f"+define+{mk}" for mk in markers("WAVES"))]
        srcs = list(files)
        if uvm:
            from .uvm import uvm_home
            home = uvm_home()
            if home is None:
                raise RuntimeError(
                    "UVM library not found: set UVM_HOME or clone "
                    "https://github.com/accellera-official/uvm-core to "
                    "~/.cache/chipchamp/uvm-core")
            build += [f"+incdir+{home}/src",
                      "+define+UVM_NO_DPI", "+define+UVM_REGEX_NO_DPI"]
            srcs = [os.path.join(home, "src", "uvm_pkg.sv")] + srcs
        for d in incdirs or []:
            build.append(f"+incdir+{d}")
        for k, v in (defines or {}).items():
            build.append(f"+define+{k}={v}")
        build += srcs
        run = [os.path.join(objdir, "Vsim")]
        for p in plusargs or []:
            run.append(f"+{p.lstrip('+')}")
        if seed is not None:
            run.append(f"+seed={seed}")
        return Plan(kind="sim", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=build, cwd=workdir),
                           Step(argv=run, cwd=workdir, allow_fail=True)],
                    artifacts={"waves": vcd} if waves else {},
                    meta={"tb_top": tb_top, "seed": seed, "uvm": uvm})

    def sim_cpp(self, rtl_files: list[str], cpp_files: list[str], top: str,
                workdir: str, cpp_incdirs: list[str] | None = None,
                incdirs: list[str] | None = None, defines: dict | None = None,
                seed: int | None = None) -> Plan:
        """Verilator C++-harness sim (`--cc --exe --build`): verilate the RTL
        `top` to C++ and link a C++ testbench `main()` that drives `V<top>` and
        checks it against a composed reference model. This is the reuse path for
        system-level simulation — a component's shipped ``ref_<name>.hpp``
        golden model (self-contained, header-only) is #included by the harness
        and composed with hand-written reference behavior for the hole modules.
        `cpp_incdirs` is the C++ include path (where the ref models and the
        shared harness base live). The harness should print CHIPCHAMP_PASS /
        CHIPCHAMP_FAIL and exit 0 / non-zero."""
        objdir = os.path.abspath(os.path.join(workdir, f"obj_cpp_{top}"))
        cflags = " ".join([f"-I{os.path.abspath(d)}" for d in (cpp_incdirs or [])]
                          + ["-std=c++17"])
        build = [self.binary, "--cc", "--exe", "--build", "-j", "0", "-sv",
                 "-Wno-fatal", "--top-module", top, "--Mdir", objdir,
                 "-o", "Vsim", "-CFLAGS", cflags]
        for d in incdirs or []:
            build.append(f"+incdir+{d}")
        for k, v in (defines or {}).items():
            build.append(f"+define+{k}={v}")
        build += list(rtl_files) + list(cpp_files)
        run = [os.path.join(objdir, "Vsim")]
        if seed is not None:
            run.append(f"+seed={seed}")
        return Plan(kind="sim", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=build, cwd=workdir),
                           Step(argv=run, cwd=workdir, allow_fail=True)],
                    meta={"tb_top": top, "seed": seed, "mode": "cpp"})

    def coverage(self, files: list[str], tb_top: str, workdir: str,
                 incdirs: list[str] | None = None) -> Plan:
        """Build a coverage-instrumented binary and run it. TB must $finish.

        Mdir and the coverage file are PER TESTBENCH: a shared obj dir lets
        make reuse a stale binary from the previous TB, which then silently
        rewrites coverage.dat with the wrong test's data."""
        objdir = os.path.abspath(os.path.join(workdir, f"obj_cov_{tb_top}"))
        covfile = os.path.abspath(os.path.join(workdir, f"coverage_{tb_top}.dat"))
        # coverage is not lint: don't let a stray warning abort the build
        build = [self.binary, "--binary", "-sv", "--coverage", "-Wno-fatal",
                 "-j", "0", "--top-module", tb_top, "--Mdir", objdir,
                 "-o", "Vsim"]
        for d in incdirs or []:
            build.append(f"+incdir+{d}")
        build += list(files)
        run = [os.path.join(objdir, "Vsim"),
               f"+verilator+coverage+file+{covfile}"]
        return Plan(kind="coverage", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=build, cwd=workdir),
                           Step(argv=run, cwd=workdir, allow_fail=True)],
                    artifacts={"coverage": covfile})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        text = "\n".join(r.stdout + "\n" + r.stderr for r in results)
        diags = parse_verilator(text, category="lint" if plan.kind == "lint" else plan.kind)
        errs = [d for d in diags if d.severity == "error"]
        if plan.kind in ("lint", "elab"):
            return ToolResult(
                ok=(len(errs) == 0), kind=plan.kind, adapter=self.name,
                diagnostics=diags,
                metrics={"errors": len(errs), "warnings": len(diags) - len(errs)},
                summary=f"{len(errs)} error(s), {len(diags) - len(errs)} warning(s)")
        if plan.kind == "coverage":
            covfile = plan.artifacts.get("coverage", "")
            ok = os.path.exists(covfile) or all(r.rc == 0 for r in results)
            return ToolResult(ok=ok, kind="coverage", adapter=self.name,
                              diagnostics=diags, artifacts=plan.artifacts,
                              summary="coverage collected" if ok else "coverage failed")
        if plan.kind == "sim":
            if results[0].rc != 0:
                return ToolResult(ok=False, kind="sim", adapter=self.name,
                                  status="error", diagnostics=diags,
                                  summary="verilate/build failed",
                                  metrics={"phase": "compile"})
            run_res = results[1] if len(results) > 1 else None
            if run_res is None:
                return ToolResult(ok=False, kind="sim", adapter=self.name,
                                  status="error", summary="did not run")
            from .icarus import _status_from_log
            from .uvm import enrich_sim_result
            log = run_res.stdout + "\n" + run_res.stderr
            status, msg = _status_from_log(log, run_res)
            artifacts = {k: v for k, v in plan.artifacts.items() if os.path.exists(v)}
            result = ToolResult(
                ok=(status == "pass"), kind="sim", adapter=self.name,
                status=status, diagnostics=diags, artifacts=artifacts,
                metrics={"seed": plan.meta.get("seed")}, summary=msg)
            return enrich_sim_result(result, log)
        return ToolResult(ok=not errs, kind=plan.kind, adapter=self.name,
                          diagnostics=diags, summary="")
