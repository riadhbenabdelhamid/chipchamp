"""Design-intelligence tools (SPEC §9 group B) — all free/read."""
from __future__ import annotations

from .base import tool, truncate
from .context import ToolContext


def _hier_dict(node, depth: int) -> dict:
    d = {"inst": node.inst_name or "(top)", "module": node.module,
         "path": node.path, "params": node.params}
    if node.unresolved:
        d["unresolved"] = True
    if depth > 0 and node.children:
        d["children"] = [_hier_dict(c, depth - 1) for c in node.children]
    elif node.children:
        d["children_count"] = len(node.children)
    return d


@tool("design.hierarchy", "Elaborated design hierarchy subtree with resolved "
      "parameters. Query this instead of grepping the file tree.",
      group="design",
      schema={"type": "object", "properties": {
          "top": {"type": "string", "description": "root module (default: a top)"},
          "depth": {"type": "integer", "default": 3}}})
def design_hierarchy(ctx: ToolContext, top: str = "", depth: int = 3) -> dict:
    db = ctx.db
    top = top or (db.tops()[0] if db.tops() else "")
    if not top:
        return {"error": "no top module found", "tops": db.tops()}
    node = db.elaborate(top)
    return {"top": top, "hierarchy": truncate(_hier_dict(node, depth)),
            "provenance": f"design-db@{db.tree_rev[:12]}"}


@tool("design.module", "Module card: ports, params, clock/reset domains, "
      "sub-instances and a summary (~200 tokens).", group="design",
      schema={"type": "object", "properties": {
          "module": {"type": "string"}}, "required": ["module"]})
def design_module(ctx: ToolContext, module: str) -> dict:
    card = ctx.db.module_card(module)
    if not card:
        return {"error": f"module '{module}' not in index",
                "known": sorted(ctx.db.modules)[:40]}
    return {"card": card.__dict__, "provenance": f"design-db@{ctx.db.tree_rev[:12]}"}


@tool("design.find_instances", "Hierarchical paths where a module is instantiated.",
      group="design",
      schema={"type": "object", "properties": {"module": {"type": "string"}},
              "required": ["module"]})
def design_find_instances(ctx: ToolContext, module: str) -> dict:
    return {"module": module, "instances": ctx.db.find_instances(module)}


@tool("design.xref", "Definition and instantiation sites of an identifier.",
      group="design",
      schema={"type": "object", "properties": {"name": {"type": "string"}},
              "required": ["name"]})
def design_xref(ctx: ToolContext, name: str) -> dict:
    return truncate(ctx.db.xref(name))


@tool("design.cone", "Pruned, self-contained source slice of a signal's fan-in "
      "(or fan-out) cone, crossing module boundaries via port connections. The "
      "debugging primitive — reason over this, not whole files.", group="design",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "signal": {"type": "string"},
          "direction": {"type": "string", "enum": ["fanin", "fanout"], "default": "fanin"},
          "depth": {"type": "integer", "default": 3}},
          "required": ["module", "signal"]})
def design_cone(ctx: ToolContext, module: str, signal: str,
                direction: str = "fanin", depth: int = 3) -> dict:
    c = ctx.db.cone(module, signal, direction, depth)
    if c is None:
        return {"error": f"module '{module}' not found"}
    return truncate({"signal": c.signal, "direction": c.direction,
                     "statements": c.statements, "signals": c.signals,
                     "truncated": c.truncated,
                     "provenance": f"design-db@{ctx.db.tree_rev[:12]}"})


@tool("design.domains", "Clock/reset domain of signals in a module and any "
      "intra-module clock-domain crossings (CDC-lite).", group="design",
      schema={"type": "object", "properties": {"module": {"type": "string"}},
              "required": ["module"]})
def design_domains(ctx: ToolContext, module: str) -> dict:
    dm = ctx.db.domain_report(module)
    if not dm:
        return {"error": f"module '{module}' not found"}
    return truncate({"module": module, "clocks": sorted(dm.clocks),
                     "resets": sorted(dm.resets),
                     "signal_domain": dm.signal_domain,
                     "crossings": [{"signal": s, "from": a, "to": b, "sink": snk}
                                   for s, a, b, snk in dm.crossings]})


@tool("design.fsm", "Extracted FSM(s) of a module: states, encoding, transitions.",
      group="design",
      schema={"type": "object", "properties": {"module": {"type": "string"}},
              "required": ["module"]})
def design_fsm(ctx: ToolContext, module: str) -> dict:
    fsms = ctx.db.fsms.get(module, [])
    if not fsms:
        return {"module": module, "fsms": [], "note": "no FSM detected"}
    return {"module": module, "fsms": [
        {"state_reg": f.state_reg, "encoding": f.encoding, "states": f.states,
         "transitions": [{"from": a, "to": b, "cond": c} for a, b, c in f.transitions]}
        for f in fsms]}
