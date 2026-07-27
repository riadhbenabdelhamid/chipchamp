"""eFPGA-FABulous tools (SPEC §8.4 eFPGA-FABulous vertical): FABulous fabric +
user-design bitstream.

Maps user RTL onto an embedded-FPGA fabric (synth → nextpnr P&R → bitstream) and,
optionally, hardens the fabric to a GDSII macro. Operates on a FABulous *project*
directory (created with the FABulous CLI); ``[efpga-fabulous] project`` in config
sets the default, or pass it explicitly.
"""
from __future__ import annotations

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
    rec, res = ctx.submit(plan, adapter, input_files=[], timeout=1800)
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
    adapter = ctx.registry.for_role("efpga-fabulous")
    if not adapter or not adapter.available():
        return {"error": "FABulous not available"}
    proj = _project(ctx, project)
    if not os.path.isdir(proj):
        return {"error": f"not a FABulous project: {ctx.rel(proj)}"}
    plan = adapter.bitstream(proj, design, with_fabric=with_fabric)
    rec, res = ctx.submit(plan, adapter, is_v3=True, input_files=[], timeout=1800)
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
    rec, res = ctx.submit(plan, adapter, is_v3=True, input_files=[], timeout=5400)
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
