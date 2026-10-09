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
            defines: dict | None = None, output_dir: str | None = None) -> Plan:
        # absolute so paths are unambiguous regardless of the step's cwd
        from pathlib import Path
        out = output_dir or workdir
        Path(out).mkdir(parents=True, exist_ok=True)
        vvp_out = os.path.abspath(os.path.join(out, f"{tb_top}.vvp"))
        vcd = os.path.abspath(os.path.join(out, f"{tb_top}.vcd"))
        deps = os.path.abspath(os.path.join(out, f"{tb_top}.inputs"))
        compile_argv = ["iverilog", "-g2012", "-grelative-include",
                        "-s", tb_top, "-o", vvp_out, f"-Mall={deps}"]
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
            artifacts={**({"waves": vcd} if waves else {}), "vvp": vvp_out},
            meta={"tb_top": tb_top, "seed": seed,
                  "depfiles": [{"path": deps, "format": "lines"}]})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        from .simparse import parse_compile_run_sim, run_log
        diags = parse_icarus(run_log(results[0])) if results else []
        return parse_compile_run_sim(self.name, plan, results, diags,
                                     runtime_tool="iverilog")


# The compile-then-run verdict lives in simparse; these names stay importable
# for callers that reached in.
from .simparse import runtime_diags as _runtime_diags_impl  # noqa: E402
from .simparse import status_from_log as _status_from_log  # noqa: E402,F401


def _runtime_diags(log: str) -> list[NormalizedDiagnostic]:
    return _runtime_diags_impl(log, "iverilog")
