"""LibreLane adapter — RTL-to-GDSII on the open stack (SPEC §8.4, physical vertical).

LibreLane (the maintained successor to OpenLane 2) drives the whole flow —
synthesis → floorplan → placement → CTS → routing → DRC/LVS/antenna signoff →
GDS streamout — over OpenROAD/Yosys/Magic/KLayout/Netgen on an open PDK (sky130).
Chipchamp ships glue: it generates the flow config, runs it (via a `librelane`
binary on PATH, or the bundled devshell AppImage), and — crucially — reads
LibreLane's normalized ``metrics.json`` (~300 signoff metrics) instead of scraping
each tool's raw reports. That is the "query, don't dump" principle applied to a
gigabyte-scale physical flow: one structured metrics object, not raw GDS/DEF/DRC
databases.
"""
from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
from dataclasses import dataclass

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)

DEFAULT_APPIMAGE = os.path.expanduser(
    "~/.local/opt/librelane/librelane-devshell-x86_64.AppImage")


# ---- metrics normalizer (fixture-tested; no flow needed) --------------------


def _min_over_corners(metrics: dict, base: str):
    """Worst (min) slack across all per-corner variants of a metric."""
    vals = [v for k, v in metrics.items()
            if k == base or k.startswith(base + "__corner:")]
    nums = [v for v in vals if isinstance(v, (int, float))]
    return min(nums) if nums else None


def normalize_metrics(metrics: dict) -> dict:
    """LibreLane metrics.json -> the normalized physical-signoff facts the gates
    and evidence use. Missing metrics come back as None (not 0)."""
    setup_ws = _min_over_corners(metrics, "timing__setup__ws")
    hold_ws = _min_over_corners(metrics, "timing__hold__ws")
    magic = metrics.get("magic__drc_error__count")
    klayout = metrics.get("klayout__drc_error__count")
    drc = None if (magic is None and klayout is None) else (magic or 0) + (klayout or 0)
    lvs = metrics.get("design__lvs_error__count")
    antenna = metrics.get("antenna__violating__nets")
    return {
        "setup_ws_ns": setup_ws,
        "hold_ws_ns": hold_ws,
        "drc_violations": drc,
        "magic_drc": magic, "klayout_drc": klayout,
        "route_drc": metrics.get("route__drc_errors"),
        "lvs_errors": lvs,
        "antenna_violations": antenna,
        "die_area_um2": metrics.get("design__die__area"),
        "cell_area_um2": metrics.get("design__instance__area"),
        "utilization": metrics.get("design__instance__utilization"),
        "wirelength_um": metrics.get("route__wirelength__estimated")
                         or metrics.get("route__wirelength"),
        "power_w": metrics.get("power__internal__total"),
        "instances": metrics.get("design__instance__count"),
    }


def signoff_verdict(norm: dict) -> tuple[str, list[str]]:
    """(verdict, failing_checks). Clean only if every present check passes."""
    fails = []
    if norm.get("setup_ws_ns") is not None and norm["setup_ws_ns"] < 0:
        fails.append(f"setup timing (WNS {norm['setup_ws_ns']:.3f} ns)")
    if norm.get("hold_ws_ns") is not None and norm["hold_ws_ns"] < 0:
        fails.append(f"hold timing (WHS {norm['hold_ws_ns']:.3f} ns)")
    if norm.get("drc_violations"):
        fails.append(f"DRC ({norm['drc_violations']} violations)")
    if norm.get("lvs_errors"):
        fails.append(f"LVS ({norm['lvs_errors']} errors)")
    if norm.get("antenna_violations"):
        fails.append(f"antenna ({norm['antenna_violations']} nets)")
    return ("signoff-clean" if not fails else "signoff-fail"), fails


class LibreLaneAdapter(Adapter):
    name = "librelane"
    binary = "librelane"
    cost = "metered"

    def __init__(self, appimage: str | None = None):
        self.appimage = appimage or DEFAULT_APPIMAGE

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["pnr"], languages=["sv", "v"],
            standards=["sky130", "gf180mcu (via PDK)"],
            wave_formats=[], coverage_formats=[],
            known_gaps=["open PDKs (sky130/gf180); foundry PDKs are NDA/on-prem",
                        "signoff DRC via Magic/KLayout, LVS via Netgen — not "
                        "Calibre/StarRC (those are M2 commercial adapters)"])

    def available(self) -> bool:
        return shutil.which("librelane") is not None or os.path.exists(self.appimage)

    def version(self) -> str:
        try:
            r = self._run_devshell("librelane --version", timeout=120)
            for ln in (r.stdout + r.stderr).splitlines():
                if "LibreLane" in ln:
                    return ln.strip()
        except Exception:
            pass
        return "librelane (unknown)"

    # ---- flow ---------------------------------------------------------------

    def pnr(self, files: list[str], top: str, workdir: str, *,
            clock_port: str = "clk", clock_period: float = 10.0,
            pdk: str = "sky130A", run_tag: str = "chipchamp",
            extra: dict | None = None) -> Plan:
        os.makedirs(workdir, exist_ok=True)
        config = {
            "DESIGN_NAME": top,
            "VERILOG_FILES": [os.path.abspath(f) for f in files],
            "CLOCK_PORT": clock_port,
            "CLOCK_PERIOD": clock_period,
            "PDK": pdk,
        }
        config.update(extra or {})
        cfg_path = os.path.abspath(os.path.join(workdir, f"config.{top}.json"))
        with open(cfg_path, "w") as fh:
            json.dump(config, fh, indent=2)
        run_dir = os.path.abspath(os.path.join(workdir, "runs", run_tag))
        argv = self._flow_argv(cfg_path, workdir, run_tag)
        return Plan(kind="pnr", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)],
                    artifacts={"run_dir": run_dir,
                               "gds": os.path.join(run_dir, "final", "gds", f"{top}.gds"),
                               "metrics": os.path.join(run_dir, "final", "metrics.json")},
                    meta={"top": top, "run_tag": run_tag})

    def _flow_argv(self, cfg_path: str, workdir: str, run_tag: str) -> list[str]:
        cmd = (f"librelane --run-tag {shlex.quote(run_tag)} --overwrite "
               f"{shlex.quote(cfg_path)}")
        if shutil.which("librelane"):
            return ["bash", "-lc", f"cd {shlex.quote(workdir)} && {cmd}"]
        # bundled devshell AppImage: it reads a script from stdin
        script = f"cd {shlex.quote(workdir)} && {cmd}\n"
        return ["bash", "-lc",
                f"printf %s {shlex.quote(script)} | {shlex.quote(self.appimage)}"]

    def sta_power(self, tcl_path: str, workdir: str) -> Plan:
        """Run an OpenSTA power script in the devshell (the same OpenSTA the
        flow uses) as a reproducible job."""
        cmd = f"sta -no_init -exit {shlex.quote(os.path.abspath(tcl_path))}"
        if shutil.which("librelane") and shutil.which("sta"):
            argv = ["bash", "-lc", cmd]
        else:
            argv = ["bash", "-lc",
                    f"printf %s {shlex.quote(cmd + chr(10))} | {shlex.quote(self.appimage)}"]
        return Plan(kind="power", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=argv, cwd=workdir, allow_fail=True)],
                    artifacts={"tcl": os.path.abspath(tcl_path)},
                    meta={"flow": "opensta"})

    def _run_devshell(self, command: str, timeout: float = 60) -> StepResult:
        import subprocess
        import time
        if shutil.which("librelane"):
            argv = ["bash", "-lc", command]
        else:
            argv = ["bash", "-lc",
                    f"printf %s {shlex.quote(command + chr(10))} | {shlex.quote(self.appimage)}"]
        t0 = time.time()
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return StepResult(argv=argv, rc=p.returncode, stdout=p.stdout,
                          stderr=p.stderr, duration_s=time.time() - t0)

    # ---- parse --------------------------------------------------------------

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0] if results else None
        if plan.kind == "power":
            from ..physical.power import parse_power_report
            text = (r.stdout + "\n" + r.stderr) if r else ""
            rep = parse_power_report(text)
            ok = bool(rep.get("total")) and (r is not None and r.rc == 0)
            tot = rep.get("total", {}).get("total_w")
            return ToolResult(
                ok=ok, kind="power", adapter=self.name,
                status="ok" if ok else "error",
                metrics={"power_w": tot,
                         "clock_share_pct": rep.get("clock_share_pct"),
                         "annotation_rate_pct":
                             rep.get("annotation", {}).get("rate_pct"),
                         "report": rep},
                artifacts={k: v for k, v in plan.artifacts.items()
                           if os.path.exists(v)},
                summary=(f"total {tot} W (clock {rep.get('clock_share_pct')}%)"
                         if ok else "sta power run failed — see log"))
        metrics_path = plan.artifacts.get("metrics", "")
        if not os.path.exists(metrics_path):
            # fall back to the newest RUN_* dir (no --run-tag support / older ver)
            cands = sorted(glob.glob(os.path.join(plan.workdir, "runs", "*",
                                                  "final", "metrics.json")),
                           key=os.path.getmtime)
            metrics_path = cands[-1] if cands else ""
        if not metrics_path or not os.path.exists(metrics_path):
            tail = ((r.stdout + r.stderr)[-400:] if r else "")
            return ToolResult(ok=False, kind="pnr", adapter=self.name, status="error",
                              summary="flow did not produce metrics.json",
                              diagnostics=[NormalizedDiagnostic(
                                  tool="librelane", severity="error", category="pnr",
                                  code="FLOW-FAIL", message=tail[:300])])
        metrics = json.load(open(metrics_path))
        norm = normalize_metrics(metrics)
        verdict, fails = signoff_verdict(norm)
        gds = plan.artifacts.get("gds", "")
        if not os.path.exists(gds):
            gg = glob.glob(os.path.join(os.path.dirname(metrics_path), "gds", "*.gds"))
            gds = gg[0] if gg else ""
        diags = [NormalizedDiagnostic(
            tool="librelane", severity="error", category="signoff",
            code="SIGNOFF", message=f) for f in fails]
        return ToolResult(
            ok=(verdict == "signoff-clean"), kind="pnr", adapter=self.name,
            status=verdict, diagnostics=diags,
            metrics={**norm, "failing_checks": fails,
                     "metrics_json": metrics_path},
            artifacts={"gds": gds, "metrics": metrics_path,
                       "run_dir": os.path.dirname(os.path.dirname(metrics_path))},
            summary=(f"{verdict}: setup {norm['setup_ws_ns']}ns, "
                     f"DRC {norm['drc_violations']}, LVS {norm['lvs_errors']}, "
                     f"antenna {norm['antenna_violations']}, "
                     f"area {norm['cell_area_um2']}µm²"))
