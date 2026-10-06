"""RapidWright adapter — headless work on vendor design checkpoints.

RapidWright is AMD/Xilinx's open-source framework for reading and writing
design checkpoints (.dcp) below the vendor GUI: placement, routing, the
netlist, the device. Chipchamp uses it for what the GUI-driven tools could
not do unattended — statistics of a routed design, a picture of its
placement, an export to the FPGA Interchange Format — each as a job with
a record, a log and a JSON report.

Every action runs a worker script in its OWN process (adapters/rw_scripts):
the JVM never lives inside the agent. The script interpreter is this
Python by default, or the one named by CHIPCHAMP_RAPIDWRIGHT_PYTHON when
RapidWright is installed in a separate environment.

RapidWright is an open-source project of Advanced Micro Devices, Inc. This
adapter is an independent integration, not affiliated with or endorsed by
AMD; none of RapidWright's code is part of Chipchamp.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys

from ..brand import env as _benv
from .base import Adapter, CapabilityManifest, NormalizedDiagnostic, Plan, Step, StepResult, ToolResult

ACTIONS = {
    "stats": "dcp_stats",
    "placement": "placement_map",
    "interchange": "interchange_export",
}

INSTALL_HINT = ("RapidWright needs Java and the `rapidwright` package: "
                "pip install -e '.[rapidwright]' (first use downloads the "
                "RapidWright jars and the device file), or set "
                "CHIPCHAMP_RAPIDWRIGHT_PYTHON to an interpreter that has it")


class RapidWrightAdapter(Adapter):
    name = "rapidwright"
    binary = "java"
    cost = "cheap"
    kinds = ("rapidwright",)

    # ---- availability -------------------------------------------------------

    def python(self) -> str:
        return _benv("RAPIDWRIGHT_PYTHON", "") or sys.executable

    def _package_importable(self) -> bool:
        py = self.python()
        if os.path.realpath(py) == os.path.realpath(sys.executable):
            return importlib.util.find_spec("rapidwright") is not None
        try:
            r = subprocess.run([py, "-c", "import importlib.util,sys;"
                                "sys.exit(0 if importlib.util.find_spec('rapidwright') else 1)"],
                               capture_output=True, timeout=30)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def available(self) -> bool:
        return shutil.which("java") is not None and self._package_importable()

    def version(self) -> str:
        if not self.available():
            return "not-installed"
        try:
            r = subprocess.run([self.python(), "-c",
                                "from importlib.metadata import version;"
                                "print(version('rapidwright'))"],
                               capture_output=True, text=True, timeout=30)
            pkg = (r.stdout or "").strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            pkg = "unknown"
        return f"rapidwright {pkg}"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["dcp"], languages=[],
            standards=["dcp", "fpga-interchange"],
            known_gaps=["reads checkpoints only (implementation stays with the "
                        "vendor flow)", "device files download on first use",
                        "routing summary is structural (PIPs present), not "
                        "a vendor route_status report"])

    # ---- plans ----------------------------------------------------------------

    def plan(self, action: str, dcp: str, out_dir: str, *,
             highlights: list[dict] | None = None) -> Plan:
        if action not in ACTIONS:
            raise ValueError(f"unknown RapidWright action {action!r}; one of {sorted(ACTIONS)}")
        os.makedirs(out_dir, exist_ok=True)
        report = os.path.join(out_dir, f"rw_{action}.json")
        argv = [self.python(), "-m", f"chipchamp.adapters.rw_scripts.{ACTIONS[action]}",
                dcp, report]
        artifacts = {"report": report}
        if action == "placement":
            png = os.path.join(out_dir, "placement.png")
            argv.append(png)
            artifacts["png"] = png
            artifacts["png_zoom"] = png.replace(".png", ".zoom.png")   # when the design is small
            if highlights:
                hl = os.path.join(out_dir, "placement_highlights.json")
                with open(hl, "w") as fh:
                    json.dump(highlights, fh)
                argv.append(hl)
        elif action == "interchange":
            ifx = os.path.join(out_dir, "interchange")
            argv.append(ifx)
            artifacts["interchange_dir"] = ifx
        return Plan(kind="rapidwright", adapter=self.name, workdir=out_dir,
                    steps=[Step(argv=argv, cwd=out_dir)], artifacts=artifacts,
                    meta={"action": action, "dcp": dcp})

    # ---- results --------------------------------------------------------------

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        action = plan.meta.get("action", "")
        report_path = plan.artifacts.get("report", "")
        report: dict = {}
        if report_path and os.path.exists(report_path):
            try:
                with open(report_path) as fh:
                    report = json.load(fh)
            except (OSError, ValueError) as e:
                report = {"error": f"unreadable report: {e}"}
        else:
            tail = (results[-1].stderr if results else "")[-800:]
            report = {"error": "the RapidWright worker wrote no report"
                               + (f": {tail.strip()}" if tail.strip() else "")}
        if report.get("error"):
            return ToolResult(
                ok=False, kind="rapidwright", adapter=self.name, status="error",
                summary=f"rapidwright {action}: {report['error'][:300]}",
                diagnostics=[NormalizedDiagnostic(
                    tool="rapidwright", severity="error", category="rapidwright",
                    code="RW", message=str(report["error"])[:500])],
                metrics={"action": action}, artifacts=dict(plan.artifacts))
        metrics = {"action": action, "part": report.get("part", ""),
                   "seconds": report.get("seconds")}
        if action == "stats":
            cells, nets = report.get("cells", {}), report.get("nets", {})
            metrics.update(cells=cells.get("total"), placed=cells.get("placed"),
                           unplaced=cells.get("unplaced"), nets=nets.get("total"),
                           unrouted_nets=nets.get("unrouted"),
                           fully_routed=report.get("fully_routed"),
                           clock_regions=len(report.get("clock_regions") or {}))
            summary = (f"{cells.get('total', 0)} cells on {report.get('part', '?')}, "
                       f"{nets.get('unrouted', 0)} unrouted net(s)"
                       + ("" if report.get("fully_routed") else " — NOT fully routed"))
        elif action == "placement":
            metrics.update(placed=report.get("placed_cells"),
                           tiles=report.get("tiles_used"), bbox=report.get("bbox"),
                           highlights=report.get("highlights"))
            summary = (f"{report.get('placed_cells', 0)} cells over "
                       f"{report.get('tiles_used', 0)} tiles → {os.path.basename(report.get('png', ''))}")
        else:
            metrics.update(bytes=report.get("bytes"), top=report.get("top"))
            summary = f"interchange export of {report.get('top', 'design')}"
        return ToolResult(ok=True, kind="rapidwright", adapter=self.name,
                          status="passed", summary=summary, metrics=metrics,
                          artifacts={k: v for k, v in plan.artifacts.items()
                                     if v and os.path.exists(v)})
