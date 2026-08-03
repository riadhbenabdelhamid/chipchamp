"""FABulous adapter — the eFPGA-FABulous vertical (SPEC §8.4).

FABulous (University of Manchester) generates a customizable eFPGA fabric and maps
a user RTL design onto it: synthesis (Yosys + the fabulous techmap) → place & route
(``nextpnr-generic --uarch fabulous``) → bitstream (``bit_gen``). The generated
fabric HDL can then be hardened to a GDSII macro (``run_FABulous_eFPGA_macro``,
which drives LibreLane — the physical vertical). Chipchamp ships glue: it invokes
the FABulous CLI flow and normalizes the results (bitstream produced, routed,
fabric generated) for the eFPGA-FABulous gates and evidence.
"""
from __future__ import annotations

import os
import shutil

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)


class FabulousAdapter(Adapter):
    name = "fabulous"
    binary = "FABulous"
    cost = "metered"
    version_flags = ("--version",)

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["efpga-fabulous"], languages=["v", "vhdl"],
            standards=["FABulous fabric"],
            known_gaps=["fabric authored via FABulous CSV/Tile specs (not RTL)",
                        "hardening reuses LibreLane (open PDK) via "
                        "run_FABulous_eFPGA_macro"])

    def available(self) -> bool:
        return shutil.which("FABulous") is not None

    # ---- flow ----------------------------------------------------------------

    def flow(self, project: str, commands: list[str], *,
             artifacts: dict | None = None, meta: dict | None = None) -> Plan:
        argv = ["FABulous", "-p", project, "run", "; ".join(commands)]
        return Plan(kind="efpga-fabulous", adapter=self.name, workdir=project,
                    steps=[Step(argv=argv, cwd=project, allow_fail=True)],
                    artifacts=artifacts or {}, meta={"commands": commands, **(meta or {})})

    def gen_fabric(self, project: str) -> Plan:
        return self.flow(project, ["run_FABulous_fabric"],
                         artifacts={"fabric_top": os.path.join(project, "Fabric", "eFPGA.v")},
                         meta={"step": "fabric"})

    def bitstream(self, project: str, design: str,
                  with_fabric: bool = False) -> Plan:
        base = os.path.splitext(design)[0]  # e.g. user_design/sequential_16bit_en
        cmds = (["run_FABulous_fabric"] if with_fabric else []) + \
               [f"run_FABulous_bitstream {design}"]
        return self.flow(project, cmds, artifacts={
            "bitstream": os.path.join(project, f"{base}.bin"),
            "fasm": os.path.join(project, f"{base}.fasm"),
            "npnr_log": os.path.join(project, f"{base}_npnr_log.txt"),
            "fabric_top": os.path.join(project, "Fabric", "eFPGA.v")},
            meta={"step": "bitstream", "design": design})

    def harden(self, project: str) -> Plan:
        """Harden the generated fabric to a GDSII macro via LibreLane."""
        return self.flow(project, ["run_FABulous_eFPGA_macro"],
                         meta={"step": "harden"})

    def simulate(self, project: str, fmt: str = "vcd",
                 bitstream: str = "") -> Plan:
        """Simulate the PROGRAMMED fabric: the generated eFPGA netlist with
        the bitstream loaded into its config chain, against the raw user
        design as a cycle-by-cycle gold model (the template testbench's own
        structure). A pass IS bitstream-vs-RTL equivalence, behaviorally."""
        cmd = f"run_simulation {fmt} {bitstream}".strip()
        base = os.path.splitext(bitstream)[0].split("/")[-1] if bitstream else ""
        arts = {"waveform": os.path.join(project, "Test", "build",
                                         f"{base}.{fmt}")} if base else {}
        return self.flow(project, [cmd], artifacts=arts,
                         meta={"step": "simulate", "design": base})

    # ---- parse ---------------------------------------------------------------

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        r = results[0] if results else None
        log = (r.stdout + "\n" + r.stderr) if r else ""
        ok_flow = bool(r) and r.rc == 0 and "executed successfully" in log
        metrics: dict = {"step": plan.meta.get("step")}

        bit = plan.artifacts.get("bitstream")
        if bit:
            metrics["bitstream_bytes"] = os.path.getsize(bit) if os.path.exists(bit) else 0
        fab_top = plan.artifacts.get("fabric_top")
        if fab_top:
            metrics["fabric_generated"] = os.path.exists(fab_top)
            if os.path.exists(fab_top):
                metrics["fabric_files"] = len(_glob_v(os.path.dirname(fab_top))) + \
                    len(_glob_v(os.path.join(os.path.dirname(os.path.dirname(fab_top)), "Tile")))
        npnr = plan.artifacts.get("npnr_log")
        if npnr and os.path.exists(npnr):
            t = open(npnr, errors="replace").read()
            metrics["routed"] = ("Program finished normally" in t
                                 and "failed to route" not in t.lower())
            # checkpoint-style timing from the embedded nextpnr run (fpga.ppa
            # compatible): fmax per clock on the mapped user design
            from ..physical.fpga import parse_nextpnr_log_fmax
            fmax = parse_nextpnr_log_fmax(t)
            if fmax:
                worst = min(fmax.values(),
                            key=lambda v: v["achieved_mhz"] - v["constraint_mhz"])
                metrics["fmax_mhz"] = worst["achieved_mhz"]
                metrics["fmax_constraint_mhz"] = worst["constraint_mhz"]
                metrics["fmax_met"] = worst["met"]

        # success = flow ok AND (bitstream produced if we asked for one)
        ok = ok_flow and (metrics.get("bitstream_bytes", 1) > 0)
        if plan.meta.get("step") == "simulate":
            metrics["sim_passed"] = ok
            if not ok:
                metrics["fail_reason"] = _fail_reason(log)
        if plan.meta.get("step") == "bitstream" and not ok:
            # "FAILED: 0 bytes" without the WHY sent a live agent into a
            # wild-goose diagnosis while the real reason sat in the log
            metrics["fail_reason"] = _fail_reason(log)
        # A known environment incompatibility must be NAMED, not dumped as a
        # per-tile error storm. Two live agent runs anchored on harden's
        # yosys crash and built the same false theory — that the tile-macro
        # flow gates place-and-route — burning their step budget on a flow
        # that is not even on the bitstream path.
        if plan.meta.get("step") == "harden" and not ok \
                and "Option 'y' does not exist" in log:
            metrics["env_incompatibility"] = "yosys_dash_y"
        diags = []
        if not ok:
            diags.append(NormalizedDiagnostic(
                tool="fabulous", severity="error", category="efpga-fabulous",
                code="EFPGA-FABULOUS-FLOW", message=_tail_error(log)))
        artifacts = {k: v for k, v in plan.artifacts.items()
                     if isinstance(v, str) and os.path.exists(v)}
        return ToolResult(
            ok=ok, kind="efpga-fabulous", adapter=self.name,
            status="ok" if ok else "error", diagnostics=diags, metrics=metrics,
            artifacts=artifacts,
            summary=_summary(plan.meta.get("step"), metrics, ok))


def _glob_v(d: str) -> list[str]:
    import glob
    return glob.glob(os.path.join(d, "**", "*.v"), recursive=True) if os.path.isdir(d) else []


_DECISIVE = ("Unable to place cell", "no BELs remaining",
             "Failed to find a route", "Routing design failed",
             "Re-definition of module", "InvalidFileType",
             "Option 'y' does not exist")


def _fail_reason(log: str) -> str:
    """The FIRST line matching a known-decisive failure signature — the line
    a human debugging this flow would quote — falling back to the last
    error-ish line."""
    for ln in log.splitlines():
        if any(sig in ln for sig in _DECISIVE):
            return ln.strip()[:200]
    return _tail_error(log)


def _tail_error(log: str) -> str:
    for ln in reversed(log.splitlines()):
        if any(w in ln.lower() for w in ("error", "fatal", "failed", "traceback")):
            return ln.strip()[:300]
    return log[-300:]


def _summary(step: str, m: dict, ok: bool) -> str:
    if step == "bitstream":
        out = (f"bitstream {'ok' if ok else 'FAILED'}: "
               f"{m.get('bitstream_bytes', 0)} bytes, routed={m.get('routed')}")
        if not ok and m.get("fail_reason"):
            out += f" — {m['fail_reason'][:140]}"
        return out
    if step == "fabric":
        return f"fabric {'generated' if m.get('fabric_generated') else 'FAILED'} " \
               f"({m.get('fabric_files', '?')} HDL files)"
    if step == "simulate":
        if ok:
            return "fabric-vs-RTL cosim PASSED: the programmed fabric matched " \
                   "the RTL gold model cycle-for-cycle"
        return "fabric-vs-RTL cosim FAILED" + \
               (f" — {m['fail_reason'][:140]}" if m.get("fail_reason") else "")
    if step == "harden":
        if ok:
            return "fabric hardened to GDSII macro"
        if m.get("env_incompatibility") == "yosys_dash_y":
            return ("harden UNAVAILABLE in this environment: the installed "
                    "yosys removed the -y flag FABulous passes. This affects "
                    "ONLY GDSII hardening — fabric generation and bitstream "
                    "routing are independent of it and unaffected.")
        return "harden FAILED"
    return ("ok" if ok else "failed")
