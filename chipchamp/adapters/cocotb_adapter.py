"""cocotb adapter (SPEC §8.4, playbook P4).

cocotb testbenches are Python coroutines that need cocotb's *own* interpreter
(the one with the compiled VPI), which is generally not the venv running
Chipchamp. So this adapter shells out to that interpreter (auto-detected:
``$CHIPCHAMP_COCOTB_PYTHON`` → ``tabbypy3`` (oss-cad-suite) → ``python3``) running
a small driver that invokes ``cocotb_tools.runner`` over the configured HDL
simulator, then parses the xUnit ``results.xml`` cocotb emits. PYTHONPATH is set
to the test directory so the sim subprocess can import the test module.
"""
from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET

from ..brand import markers as _markers
from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)

_DRIVER = r'''
import sys, json
from cocotb_tools.runner import get_runner
cfg = json.loads(sys.argv[1])
runner = get_runner(cfg["sim"])
runner.build(sources=cfg["sources"], hdl_toplevel=cfg["top"],
             build_dir=cfg["build_dir"], always=True,
             timescale=tuple(cfg["timescale"]), waves=cfg["waves"])
runner.test(hdl_toplevel=cfg["top"], test_module=cfg["module"],
            test_dir=cfg["test_dir"], build_dir=cfg["build_dir"],
            results_xml=cfg["results_xml"], seed=cfg.get("seed"))
'''


def _cocotb_python() -> str | None:
    from ..brand import env as benv
    for cand in (benv("COCOTB_PYTHON"), "tabbypy3", "python3"):
        if cand and shutil.which(cand):
            # verify cocotb importable
            import subprocess
            try:
                r = subprocess.run([cand, "-c", "import cocotb_tools.runner"],
                                   capture_output=True, timeout=30)
                if r.returncode == 0:
                    return cand
            except (OSError, subprocess.SubprocessError):
                continue
    return None


class CocotbAdapter(Adapter):
    name = "cocotb"
    binary = "tabbypy3"  # nominal; real resolution via _cocotb_python()
    cost = "cheap"

    def available(self) -> bool:
        return _cocotb_python() is not None

    def version(self) -> str:
        py = _cocotb_python()
        if not py:
            return "not-installed"
        import subprocess
        try:
            out = subprocess.run([py, "-c", "import cocotb; print(cocotb.__version__)"],
                                 capture_output=True, text=True, timeout=30)
            return f"cocotb {out.stdout.strip()}"
        except (OSError, subprocess.SubprocessError):
            return "cocotb (unknown)"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["sim"], languages=["sv", "python"],
            standards=["cocotb"], wave_formats=["vcd", "fst"], xprop=True,
            known_gaps=["requires cocotb's interpreter (auto-detected)",
                        "backed by an HDL simulator (icarus/verilator)"])

    def sim(self, files: list[str], test_module: str, top: str, workdir: str, *,
            hdl_sim: str = "icarus", seed: int | None = None, waves: bool = True,
            test_dir: str | None = None,
            timescale: tuple[str, str] = ("1ns", "1ps")) -> Plan:
        py = _cocotb_python() or "python3"
        build_dir = os.path.abspath(os.path.join(workdir, "cocotb_build"))
        results = os.path.abspath(os.path.join(workdir, f"{test_module}.results.xml"))
        test_dir = os.path.abspath(test_dir or workdir)
        import json as _json
        cfg = {"sim": hdl_sim, "sources": [os.path.abspath(f) for f in files],
               "top": top, "module": test_module, "build_dir": build_dir,
               "test_dir": test_dir, "results_xml": results,
               "timescale": list(timescale), "waves": waves, "seed": seed}
        driver = os.path.join(build_dir, "_chipchamp_cocotb_driver.py")
        os.makedirs(build_dir, exist_ok=True)
        with open(driver, "w") as fh:
            fh.write(_DRIVER)
        cov_xml = os.path.abspath(os.path.join(workdir, f"{test_module}.cov.xml"))
        env = {"PYTHONPATH": test_dir + os.pathsep + os.environ.get("PYTHONPATH", ""),
               # cocotb-coverage TBs export here (absolute: the sim's cwd is
               # the runner's build/test dir, not the workspace root)
               **{mk: cov_xml for mk in _markers("COV_XML")}}
        return Plan(kind="sim", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=[py, driver, _json.dumps(cfg)], cwd=workdir,
                                env=env, allow_fail=True)],
                    artifacts={"results": results, "coverage_xml": cov_xml},
                    meta={"module": test_module, "seed": seed})

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0]
        xml = plan.artifacts.get("results", "")
        diags: list[NormalizedDiagnostic] = []
        n_pass = n_fail = 0
        if os.path.exists(xml):
            try:
                tree = ET.parse(xml)
                for tc in tree.iter("testcase"):
                    fail = tc.find("failure") if tc.find("failure") is not None \
                        else tc.find("error")
                    if fail is not None:
                        n_fail += 1
                        diags.append(NormalizedDiagnostic(
                            tool="cocotb", severity="error", category="sim",
                            code=fail.get("error_type", "COCOTB-FAIL"),
                            message=f"{tc.get('classname')}.{tc.get('name')}: "
                                    f"{fail.get('error_msg', fail.text or '')[:200]}"))
                    else:
                        n_pass += 1
            except ET.ParseError:
                pass
        status = "pass" if (n_fail == 0 and n_pass > 0) else (
            "fail" if n_fail else "error")
        if n_pass == 0 and n_fail == 0:
            status = "error"
            diags.append(NormalizedDiagnostic(
                tool="cocotb", severity="error", category="sim", code="COCOTB",
                message="no results.xml / no tests ran (see log)"))
        artifacts = {k: v for k, v in plan.artifacts.items() if os.path.exists(v)}
        return ToolResult(
            ok=(status == "pass"), kind="sim", adapter=self.name, status=status,
            diagnostics=diags, artifacts=artifacts,
            metrics={"passed": n_pass, "failed": n_fail, "seed": plan.meta.get("seed")},
            summary=f"cocotb: {n_pass} passed, {n_fail} failed")
