"""eFPGA-FABulous tools (SPEC §8.4 eFPGA-FABulous vertical): FABulous fabric +
user-design bitstream.

Maps user RTL onto an embedded-FPGA fabric (synth → nextpnr P&R → bitstream) and,
optionally, hardens the fabric to a GDSII macro. Operates on a FABulous *project*
directory (created with the FABulous CLI); ``[efpga-fabulous] project`` in config
sets the default, or pass it explicitly.
"""
from __future__ import annotations

import glob
import os

from .base import tool, truncate
from .context import ToolContext


def _project(ctx: ToolContext, project: str = "") -> str:
    if project:
        return project if os.path.isabs(project) else os.path.join(ctx.ws.root, project)
    cfg = (ctx.ws.config.get("efpga-fabulous", {}) or {}).get("project")
    if cfg:
        return cfg if os.path.isabs(cfg) else os.path.join(ctx.ws.root, cfg)
    return os.path.join(ctx.ws.root, "efpga-fabulous")


def _efpga_inputs(proj: str, design: str = "") -> list[str]:
    """The honest input manifest for an eFPGA job.

    Every eFPGA job used to declare ``input_files=[]``, so ALL of them shared
    the one constant empty-manifest hash. Two bitstream jobs straddling a
    fabric corruption therefore looked like "identical inputs, one passed,
    one failed" — a live agent read exactly that and diagnosed nextpnr
    nondeterminism on a perfectly deterministic fault. The manifest is the
    fabric DEFINITION (source CSVs/lists), the generated routing graph, and
    for a mapping job the design files being placed onto it."""
    paths: list[str] = []
    for pat in ("fabric.csv", "Tile/**/*.csv", "Tile/**/*.list"):
        paths.extend(glob.glob(os.path.join(proj, pat), recursive=True))
    if design:
        paths.extend(glob.glob(os.path.join(proj, ".FABulous", "pips.txt")))
        d = os.path.join(proj, design)
        if os.path.isfile(d):
            paths.append(d)
        paths.extend(glob.glob(os.path.join(proj, "user_design", "*.v")))
    return sorted(set(paths))


def _latest_efpga(ctx: ToolContext):
    for rec in reversed(ctx.task_jobs):
        if rec.kind == "efpga-fabulous":
            return rec
    for rec in reversed(ctx.runner.list_jobs()):
        if rec.kind == "efpga-fabulous":
            return rec
    return None


@tool("efpga-fabulous.fabric",
      "Generate the eFPGA-FABulous fabric HDL for a FABulous project "
      "(run_FABulous_fabric).", cost="metered", permission="submit",
      group="efpga-fabulous",
      schema={"type": "object", "properties": {"project": {"type": "string"}}})
def efpga_fabric(ctx: ToolContext, project: str = "") -> dict:
    adapter = ctx.registry.for_role("efpga-fabulous")
    if not adapter or not adapter.available():
        return {"error": "FABulous not available (pip install FABulous)"}
    proj = _project(ctx, project)
    if not os.path.isdir(proj):
        return {"error": f"not a FABulous project: {ctx.rel(proj)} "
                "(create one with `FABulous create-project`)"}
    plan = adapter.gen_fabric(proj)
    rec, res = ctx.submit(plan, adapter, input_files=_efpga_inputs(proj),
                          timeout=1800)
    return {"job": rec.id, "status": rec.status, "summary": res.summary,
            "fabric_files": res.metrics.get("fabric_files")}


@tool("efpga-fabulous.bitstream",
      "Map a user RTL design onto the eFPGA-FABulous fabric: synthesis → "
      "nextpnr place & route → bitstream. Returns the bitstream path + whether "
      "it routed.", cost="metered", permission="submit",
      group="efpga-fabulous",
      schema={"type": "object", "properties": {
          "design": {"type": "string",
                     "description": "design path relative to the project, e.g. "
                                    "user_design/foo.v"},
          "project": {"type": "string"},
          "with_fabric": {"type": "boolean", "default": False,
                          "description": "regenerate the fabric first"}},
          "required": ["design"]})
def efpga_bitstream(ctx: ToolContext, design: str, project: str = "",
                    with_fabric: bool = False) -> dict:
    # Argument teaching BEFORE anything else — two live campaigns produced
    # exactly these calls, and the downstream yosys/FABulous errors they
    # cause ("Re-definition of module top_wrapper", InvalidFileType) read
    # like fabric breakage and send agents chasing ghosts.
    if os.path.splitext(os.path.basename(design))[0] == "top_wrapper":
        return {"error": "top_wrapper.v is the pad-ring WRAPPER, not the user "
                         "design — it instantiates the design by module name "
                         "and is included automatically. Pass the design "
                         "file, e.g. design='user_design/<your_design>.v'."}
    if not design.endswith((".v", ".sv", ".vhd", ".vhdl")):
        return {"error": f"'{design}' is not a design file path — pass the "
                         f"HDL file relative to the project, e.g. "
                         f"'user_design/{design}.v', not a bare module name."}
    adapter = ctx.registry.for_role("efpga-fabulous")
    if not adapter or not adapter.available():
        return {"error": "FABulous not available"}
    proj = _project(ctx, project)
    if not os.path.isdir(proj):
        return {"error": f"not a FABulous project: {ctx.rel(proj)}"}
    plan = adapter.bitstream(proj, design, with_fabric=with_fabric)
    rec, res = ctx.submit(plan, adapter, is_v3=True,
                          input_files=_efpga_inputs(proj, design), timeout=1800)
    return truncate({
        "job": rec.id, "status": rec.status, "verdict": res.status,
        "bitstream": ctx.rel(res.artifacts["bitstream"]) if res.artifacts.get("bitstream") else None,
        "bitstream_bytes": res.metrics.get("bitstream_bytes"),
        "routed": res.metrics.get("routed"),
        "summary": res.summary})


@tool("efpga-fabulous.harden",
      "Harden the generated fabric to a GDSII macro via LibreLane "
      "(run_FABulous_eFPGA_macro). Long-running.", cost="metered",
      permission="submit", group="efpga-fabulous",
      schema={"type": "object", "properties": {"project": {"type": "string"}}})
def efpga_harden(ctx: ToolContext, project: str = "") -> dict:
    adapter = ctx.registry.for_role("efpga-fabulous")
    if not adapter or not adapter.available():
        return {"error": "FABulous not available"}
    proj = _project(ctx, project)
    plan = adapter.harden(proj)
    rec, res = ctx.submit(plan, adapter, is_v3=True,
                          input_files=_efpga_inputs(proj), timeout=5400)
    return {"job": rec.id, "status": rec.status, "summary": res.summary}


@tool("efpga-fabulous.info",
      "Query the latest eFPGA-FABulous job (bitstream size, routed, fabric "
      "files) — don't dump the fabric HDL.", group="efpga-fabulous",
      schema={"type": "object", "properties": {"job": {"type": "string"}}})
def efpga_info(ctx: ToolContext, job: str = "") -> dict:
    rec = ctx.runner.get(job) if job else _latest_efpga(ctx)
    if not rec or rec.kind != "efpga-fabulous":
        return {"error": "no eFPGA-FABulous job found"}
    return {"job": rec.id, "status": rec.status, **rec.result.get("metrics", {})}
