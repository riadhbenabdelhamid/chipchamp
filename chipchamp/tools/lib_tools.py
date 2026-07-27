"""IP library tools: search → inspect → fetch verified building blocks
(SPEC §12 reuse posture: prefer a qualified component over hand-written RTL).

The system prompt carries only the library INDEX (library + category counts);
cards load through ``lib.info`` when the model picks a component. ``lib.fetch``
copies the component's RTL fileset into the workspace through the policy gate —
every written file is a recorded edit, so gating sees fetched IP like any
other change. Library metadata is data, not authority.
"""
from __future__ import annotations

from ..brand import marker
import os

from ..library import discover, files_for, instantiation, module_card, search
from .base import tool, truncate
from .context import ToolContext


def _close_matches(name: str, known) -> list[str]:
    import difflib
    return difflib.get_close_matches(name, list(known), n=3, cutoff=0.5)


@tool("lib.list", "List the registered IP libraries and their category map "
      "(counts per area: storage, bus, noc, peripherals…). The compact form "
      "of this rides in the system prompt.", group="library",
      schema={"type": "object", "properties": {
          "category": {"type": "string",
                       "description": "only this category subtree (e.g. 'storage')"}}})
def lib_list(ctx: ToolContext, category: str = "") -> dict:
    mods = discover(ctx.ws)
    rows = [{"name": m.name, "category": m.category, "status":
             m.meta.get("status", ""), "summary": m.summary}
            for m in sorted(mods.values(), key=lambda x: (x.category, x.name))
            if not category or m.category.lower().startswith(category.lower())]
    return truncate({"components": rows, "total": len(rows)}, max_items=150)


@tool("lib.search", "Search the IP library for a building block by keywords "
      "(name/tags/category/summary). Call this BEFORE writing a standard "
      "block (FIFO, arbiter, CDC sync, UART, AXI…) from scratch — a verified, "
      "parameterizable component probably exists. Then lib.info(name) for the "
      "card.", group="library",
      schema={"type": "object", "properties": {
          "query": {"type": "string"},
          "category": {"type": "string",
                       "description": "restrict to a category prefix, e.g. 'bus/axi'"},
          "limit": {"type": "integer", "default": 8}},
          "required": ["query"]})
def lib_search(ctx: ToolContext, query: str, category: str = "",
               limit: int = 8) -> dict:
    hits = search(ctx.ws, query=query, category=category, limit=limit)
    if not hits:
        return {"query": query, "matches": [],
                "hint": "no match — lib.list shows the category map"}
    return truncate({"query": query, "matches": [
        {"name": m.name, "category": m.category, "summary": m.summary,
         "status": m.meta.get("status", ""), "tags": m.meta.get("tags", [])}
        for m in hits]})


@tool("lib.match", "Find library components whose FUNCTION and INTERFACE match "
      "a sub-block you describe — even when the names differ. Describe what the "
      "block DOES (behavior) and its interface (protocol / ports / handshake / "
      "clocks); returns ranked candidates by semantic + interface similarity, "
      "each with a match rationale and interface signature so you can judge "
      "'close enough' and map ports. Use this per node when decomposing an "
      "architecture — it catches parts that keyword lib.search misses.",
      group="library",
      schema={"type": "object", "properties": {
          "behavior": {"type": "string",
                       "description": "what the sub-block does, in your own words"},
          "interface": {"type": "string",
                        "description": "optional: protocol/ports/handshake/clocks "
                                       "(e.g. 'valid/ready sink+source, single clock')"},
          "category": {"type": "string",
                       "description": "optional category prefix, e.g. 'storage'"},
          "limit": {"type": "integer", "default": 6}},
          "required": ["behavior"]})
def lib_match(ctx: ToolContext, behavior: str, interface: str = "",
              category: str = "", limit: int = 6) -> dict:
    from ..library_semantic import match
    try:
        hits = match(ctx.ws, behavior, interface=interface, category=category,
                     limit=limit)
    except Exception as e:
        return {"error": f"match failed: {type(e).__name__}: {e}",
                "hint": "fall back to lib.search(keywords)"}
    if not hits:
        return {"behavior": behavior, "matches": [],
                "hint": "no components — check `lib.list` or that a library is registered"}
    return truncate({"behavior": behavior, "interface": interface, "matches": [
        {"name": m.name, "category": m.category,
         "status": m.meta.get("status", ""), "summary": m.summary,
         "score": round(sc, 3), "semantic": info["semantic"],
         "interface_match": info["interface_score"],
         "interface": info["interface_signature"],
         "why": info["why"] or ["functional similarity"]}
        for m, sc, info in hits],
        "note": "then lib.info(name) for params + instantiation, lib.fetch(name) "
                "to copy it in; confirm the interface maps before reusing"})


@tool("lib.info", "Full card for one library component: parameters (with "
      "ranges/defaults), clocks/resets, ports, verification status, fileset, "
      "and a ready-to-edit instantiation template.", group="library",
      schema={"type": "object", "properties": {
          "name": {"type": "string"}}, "required": ["name"]})
def lib_info(ctx: ToolContext, name: str) -> dict:
    mods = discover(ctx.ws)
    m = mods.get(name)
    if m is None:
        close = _close_matches(name, mods)
        return {"error": f"unknown component '{name}'"
                + (f" — did you mean: {', '.join(close)}?" if close
                   else " — lib.search finds components by keyword")}
    return truncate(module_card(m), max_items=80, max_str=6000)


@tool("lib.fetch", "Copy a library component into the workspace. view='rtl' "
      "(default) fetches its RTL fileset (default rtl/lib/) to instantiate. "
      "view='sim' fetches its C++ golden reference model ref_<name>.hpp + the "
      "shared harness base (default tb/verilator/) for a SYSTEM-LEVEL Verilator "
      "sim — #include the ref model in your harness and compose it. Policy-"
      "gated writes; identical files left untouched.",
      permission="write", cost="cheap", group="library",
      schema={"type": "object", "properties": {
          "name": {"type": "string"},
          "view": {"type": "string", "enum": ["rtl", "sim"], "default": "rtl",
                   "description": "rtl = synthesizable fileset; sim = C++ ref model"},
          "dest": {"type": "string",
                   "description": "workspace-relative dest dir (default "
                                  "<work_dir>/rtl/lib for rtl, "
                                  "<work_dir>/tb/verilator for sim)"}},
          "required": ["name"]})
def lib_fetch(ctx: ToolContext, name: str, view: str = "rtl",
              dest: str = "") -> dict:
    from ..library import sim_ref_files
    mods = discover(ctx.ws)
    m = mods.get(name)
    if m is None:
        close = _close_matches(name, mods)
        return {"error": f"unknown component '{name}'"
                + (f" — did you mean: {', '.join(close)}?" if close else "")}
    sim = view == "sim"
    dest = (dest or "").strip("/")
    if not dest:
        # Default INSIDE the agent work dir: the work dir is always part of an
        # inferred target's live roots, so fetched RTL is compiled (and its
        # .svh dirs join the include path) automatically — a root-level
        # rtl/lib can sit outside the target's globs and outside an explicit
        # paths=["work/..."] lint, leaving instantiations MODMISSING.
        wd = os.path.relpath(str(ctx.ws.work_dir), ctx.ws.root)
        sub = "tb/verilator" if sim else "rtl/lib"
        dest = sub if wd in (".", "") else os.path.join(wd, sub)
    src_files = sim_ref_files(m) if sim else files_for(m)
    if sim and not src_files:
        return {"error": f"'{name}' ships no C++ reference model (ref_<name>.hpp) "
                         f"— check lib.info(name)['verification']"}
    written, unchanged, denied = [], [], []
    for f in src_files:
        rel = os.path.join(dest, f["rel"])
        ok, why = ctx.policy.can_write(rel)
        if not ok:
            denied.append({"path": rel, "reason": why})
            continue
        ap = rel if os.path.isabs(rel) else os.path.join(ctx.ws.root, rel)
        new = open(f["src"], errors="replace").read()
        old = open(ap, errors="replace").read() if os.path.exists(ap) else ""
        if old == new:
            unchanged.append(rel)
            continue
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w") as fh:
            fh.write(new)
        ctx.record_edit(rel, old, new, "modified" if old else "added")
        written.append(rel)
    if written:
        ctx.ws.invalidate(ctx.target_name)
    out = {"component": name, "library": m.lib, "view": view, "dest": dest,
           "written": written, "unchanged": unchanged}
    if sim:
        out["note"] = (f"C++ golden model fetched. In your Verilator harness "
                       f"`#include \"ref_{name}.hpp\"` (and \"tb_base.hpp\"), "
                       f"compose it with hole refs, and run via a tests.yaml "
                       f"entry with runner kind 'verilator_cpp' (top=<your RTL "
                       f"top>, cpp=[<harness.cpp>], cpp_incdirs=['{dest}']). The "
                       f"harness must print {marker('PASS')} / {marker('FAIL')}.")
    else:
        out["instantiation"] = instantiation(m)
        out["note"] = (f"the fetched .sv files are compiled AUTOMATICALLY (the "
                       f"work dir is in the target, '{dest}' is on the include "
                       f"path for .svh headers). INSTANTIATE the module using "
                       f"the template — do NOT `include \"{name}.sv\"` (that "
                       f"compiles it twice and duplicate-declares it).")
    if denied:
        out["denied"] = denied
    return truncate(out, max_items=80)
