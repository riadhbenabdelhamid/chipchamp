"""FPGA flow adapter — yosys synth + nextpnr P&R with stage checkpoints
(SPEC §8.4 FPGA vertical, license-free path).

Two checkpoints per run, mirroring Vivado's DCP stages on the open flow:
``post_synth`` (yosys netlist JSON + stat) and ``post_route`` (nextpnr
placed+routed JSON + ``--report`` metrics). The report carries fmax per clock,
utilization per bel, and critical paths with **native RTL source refs** (yosys
``src`` attributes survive P&R) — the FPGA cross-probe comes free.
"""
from __future__ import annotations

import json
import os
import shutil

from .base import (Adapter, CapabilityManifest, NormalizedDiagnostic, Plan,
                   Step, StepResult, ToolResult)

# family -> (yosys synth command, nextpnr binary, default device args)
_FAMILIES = {
    "ice40": ("synth_ice40", "nextpnr-ice40",
              ["--hx8k", "--package", "ct256"]),
    "ecp5": ("synth_ecp5", "nextpnr-ecp5",
             ["--85k", "--package", "CABGA381"]),
    "machxo2": ("synth_machxo2", "nextpnr-machxo2", ["--1200"]),
    "nexus": ("synth_nexus", "nextpnr-nexus", ["--device", "LIFCL-40-9BG400CES"]),
}

# family -> (nextpnr flag that writes the packer's input, that file's name,
#            packer binary, bitstream file name). P&R alone stops at a routed
# netlist; the packer turns it into something you can actually flash.
_BITSTREAM = {
    "ice40": ("--asc", "post_route.asc", "icepack", "post_route.bin"),
    "ecp5": ("--textcfg", "post_route.config", "ecppack", "post_route.bit"),
    "machxo2": ("--textcfg", "post_route.config", "ecppack", "post_route.bit"),
    "nexus": ("--fasm", "post_route.fasm", "prjoxide", "post_route.bit"),
}


def _pack_argv(packer: str, config: str, bitstream: str) -> list[str]:
    """Each packer takes (input, output) but spells it differently."""
    if packer == "prjoxide":
        return ["prjoxide", "pack", config, bitstream]
    return [packer, config, bitstream]  # icepack / ecppack


class NextpnrAdapter(Adapter):
    name = "nextpnr"
    binary = "nextpnr-ice40"  # nominal; per-family binary chosen in fpga_flow
    cost = "cheap"

    def available(self) -> bool:
        return shutil.which("yosys") is not None and any(
            shutil.which(v[1]) for v in _FAMILIES.values())

    def families(self) -> list[str]:
        return [f for f, v in _FAMILIES.items() if shutil.which(v[1])]

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name=self.name, roles=["fpga"], languages=["sv"],
            standards=[f"nextpnr:{f}" for f in self.families()],
            known_gaps=["open-tool timing models (sign-off timing is the "
                        "vendor tool's call)", "no post-place-only checkpoint "
                        "(pack+place+route in one step)"])

    def fpga_flow(self, files: list[str], top: str, workdir: str,
                  family: str = "ice40", device_args: list[str] | None = None,
                  freq_mhz: float = 100.0,
                  incdirs: list[str] | None = None,
                  defines: dict | None = None) -> Plan:
        if family not in _FAMILIES:
            raise ValueError(f"unknown FPGA family '{family}' "
                             f"(known: {sorted(_FAMILIES)})")
        synth_cmd, pnr_bin, default_dev = _FAMILIES[family]
        ckpt = os.path.abspath(os.path.join(workdir, "checkpoints"))
        os.makedirs(ckpt, exist_ok=True)
        post_synth = os.path.join(ckpt, "post_synth.json")
        synth_stat = os.path.join(ckpt, "post_synth.stat.json")
        post_route = os.path.join(ckpt, "post_route.json")
        report = os.path.join(ckpt, "post_route.report.json")

        reads = "; ".join(f"read_verilog -sv {f}" for f in files)
        inc = "".join(f" -I{d}" for d in (incdirs or []))
        defs = "".join(f" -D{k}={v}" for k, v in (defines or {}).items())
        if inc or defs:
            reads = "; ".join(f"read_verilog -sv{inc}{defs} {f}" for f in files)
        script = (f"{reads}; {synth_cmd} -top {top} -json {post_synth}; "
                  f"tee -q -o {synth_stat} stat -json")
        pnr_argv = [pnr_bin, *(device_args or default_dev),
                    "--json", post_synth, "--write", post_route,
                    "--report", report, "--freq", str(freq_mhz)]
        artifacts = {"post_synth": post_synth,
                     "post_synth_stat": synth_stat,
                     "post_route": post_route,
                     "report": report}
        # Always ask P&R for the packer's input (was ice40-only), so
        # `fpga.bitstream` can pack afterwards without re-running the flow.
        flag, cfg_name, _packer, _bit = _BITSTREAM[family]
        cfg_path = os.path.join(ckpt, cfg_name)
        pnr_argv += [flag, cfg_path]
        artifacts["bitstream_config"] = cfg_path

        return Plan(kind="fpga", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=["yosys", "-q", "-p", script], cwd=workdir),
                           Step(argv=pnr_argv, cwd=workdir, allow_fail=True)],
                    artifacts=artifacts,
                    meta={"top": top, "family": family,
                          "freq_mhz": freq_mhz, "flow": "nextpnr"})

    def packer_for(self, family: str) -> str:
        """The bitstream packer this family needs ('' if the family is unknown)."""
        return _BITSTREAM[family][2] if family in _BITSTREAM else ""

    def pack_flow(self, config: str, family: str, workdir: str,
                  top: str = "") -> Plan:
        """Pack a routed design into a flashable bitstream. Runs only the
        packer — the P&R checkpoint it reads was written by ``fpga_flow``."""
        if family not in _BITSTREAM:
            raise ValueError(f"unknown FPGA family '{family}' "
                             f"(known: {sorted(_BITSTREAM)})")
        _flag, _cfg, packer, bit_name = _BITSTREAM[family]
        bitstream = os.path.join(os.path.dirname(config) or workdir, bit_name)
        return Plan(kind="fpga_bitstream", adapter=self.name, workdir=workdir,
                    steps=[Step(argv=_pack_argv(packer, config, bitstream),
                                cwd=workdir, allow_fail=True)],
                    artifacts={"bitstream": bitstream, "bitstream_config": config},
                    meta={"top": top, "family": family, "flow": "nextpnr",
                          "packer": packer})

    def _parse_pack(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        run = results[0] if results else None
        bit = plan.artifacts.get("bitstream", "")
        size = os.path.getsize(bit) if os.path.exists(bit) else 0
        packer = plan.meta.get("packer", "packer")
        diags: list[NormalizedDiagnostic] = []
        # A packer that exits 0 but writes nothing has not produced a bitstream.
        ok = bool(run and run.rc == 0 and size > 0)
        if not ok:
            tail = ((run.stderr or run.stdout).strip().splitlines() if run else [])
            diags.append(NormalizedDiagnostic(
                tool=packer, severity="error", category="fpga", code="PACK",
                message=(tail[-1] if tail else
                         f"{packer} produced no bitstream")[:200]))
        return ToolResult(
            ok=ok, kind="fpga_bitstream", adapter=self.name,
            status="pass" if ok else "fail", diagnostics=diags,
            artifacts={k: v for k, v in plan.artifacts.items()
                       if os.path.exists(v)},
            metrics={"family": plan.meta.get("family"),
                     "top": plan.meta.get("top"), "flow": "nextpnr",
                     "packer": packer, "bitstream_bytes": size},
            summary=(f"{packer}: {size} byte bitstream" if ok
                     else f"{packer} failed to produce a bitstream"))

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        from ..physical.fpga import parse_nextpnr_report, worst_fmax
        if plan.kind == "fpga_bitstream":
            return self._parse_pack(plan, results)
        diags: list[NormalizedDiagnostic] = []
        if results[0].rc != 0:
            tail = (results[0].stderr or results[0].stdout).strip().splitlines()
            diags.append(NormalizedDiagnostic(
                tool="yosys", severity="error", category="fpga", code="SYNTH",
                message=(tail[-1] if tail else "yosys synthesis failed")[:200]))
            return ToolResult(ok=False, kind="fpga", adapter=self.name,
                              status="error", diagnostics=diags,
                              summary="FPGA synthesis failed")
        pnr = results[1] if len(results) > 1 else None
        report_path = plan.artifacts.get("report", "")
        rep = parse_nextpnr_report(report_path) if os.path.exists(report_path) else {}
        metrics: dict = {"family": plan.meta.get("family"),
                         "top": plan.meta.get("top"),
                         "flow": "nextpnr",
                         "freq_target_mhz": plan.meta.get("freq_mhz"),
                         "stages": ["post_synth"]}
        routed = bool(pnr and pnr.rc == 0 and rep)
        if routed:
            metrics["stages"].append("post_route")
        wf = worst_fmax(rep) if rep else None
        if wf:
            metrics.update(clock=wf[0], fmax_mhz=wf[1],
                           fmax_constraint_mhz=wf[2],
                           fmax_met=wf[1] >= wf[2])
        for bel, v in (rep.get("utilization") or {}).items():
            metrics[f"{bel}_used"] = v["used"]
            metrics[f"{bel}_avail"] = v["available"]
        log = ((pnr.stdout + pnr.stderr) if pnr else "")
        if pnr and pnr.rc != 0:
            tail = [ln for ln in log.strip().splitlines() if "ERROR" in ln]
            diags.append(NormalizedDiagnostic(
                tool=plan.meta.get("family", "nextpnr"), severity="error",
                category="fpga", code="PNR",
                message=(tail[-1] if tail else "place&route failed")[:200]))
        ok = routed and bool(metrics.get("fmax_met", True))
        status = "pass" if ok else ("fail" if routed else "error")
        summary = (f"{plan.meta.get('family')}: fmax {metrics.get('fmax_mhz', '?')} MHz "
                   f"vs {metrics.get('fmax_constraint_mhz', '?')} target — "
                   f"{'MET' if metrics.get('fmax_met') else 'checkpoint failed' if not routed else 'NOT MET'}"
                   if routed else "P&R did not complete")
        artifacts = {k: v for k, v in plan.artifacts.items() if os.path.exists(v)}
        return ToolResult(ok=ok, kind="fpga", adapter=self.name, status=status,
                          diagnostics=diags, artifacts=artifacts,
                          metrics=metrics, summary=summary)
