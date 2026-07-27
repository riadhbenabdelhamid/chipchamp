"""Static-quality + backend tools: lint, CDC-lite, format, synth, LEC (SPEC §9 C/H)."""
from __future__ import annotations

import os

from .base import tool, truncate
from .context import ToolContext


def _target_files(ctx: ToolContext, paths=None):
    if not paths:
        return ctx.target.sources
    from ..config import _glob_rtl
    out = []
    for p in paths:
        ap = p if os.path.isabs(p) else os.path.join(ctx.ws.root, p)
        if os.path.isdir(ap):
            out.extend(_glob_rtl([ap]))  # a directory arg → its HDL files
        else:
            out.append(ap)
    return out


def _realpath(ctx: ToolContext, f: str) -> str:
    return os.path.realpath(f if os.path.isabs(f) else os.path.join(ctx.ws.root, f))


@tool("lint.run", "Run the configured linter(s); normalized diagnostics.",
      cost="cheap", permission="submit", group="quality",
      schema={"type": "object", "properties": {
          "paths": {"type": "array", "items": {"type": "string"}},
          "top": {"type": "string"}}})
def lint_run(ctx: ToolContext, paths=None, top: str = "") -> dict:
    adapter = ctx.registry.for_role("lint")
    if not adapter or not adapter.available():
        return {"error": "no lint adapter available"}
    requested = _target_files(ctx, paths)  # abs paths, directories expanded
    # Compile the WHOLE target so packages and sub-modules always resolve; when
    # a subset was requested, report only those files' diagnostics. A subset
    # lint must not force the caller to also list every package/dependency the
    # subset imports (a fetched library component imports rtllib_pillars_pkg).
    if paths:
        tset = {_realpath(ctx, f) for f in ctx.target.sources}
        compile_files = list(ctx.target.sources) + [
            f for f in requested if _realpath(ctx, f) not in tset]
    else:
        compile_files = requested
    top = top or ctx.target.top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    if adapter.name == "verilator":
        plan = adapter.lint(compile_files, ctx.ws.root, top=top or None,
                            incdirs=ctx.target.incdirs)
    else:
        plan = adapter.lint(compile_files, ctx.ws.root)
    rec, res = ctx.submit(plan, adapter, input_files=compile_files, timeout=120)
    # waivers are data, applied at the platform layer (FR-ADPT-03, §12.6)
    from ..policy.waivers import apply_waivers, load_waivers
    wstats = apply_waivers(res.diagnostics, load_waivers(ctx.ws.root), ctx.ws.root)
    # persist the FULL diagnostic set into the job record so gates see the
    # whole design, even when the report below is filtered to a subset
    rec.result["diagnostics"] = [d.__dict__ for d in res.diagnostics]
    ctx.runner._persist(rec)

    reported = res.diagnostics
    other_errors = 0
    if paths:
        want = {_realpath(ctx, f) for f in requested}
        reported = [d for d in res.diagnostics if _realpath(ctx, d.file) in want]
        other_errors = sum(1 for d in res.diagnostics if d.severity == "error"
                           and d.waiver is None and _realpath(ctx, d.file) not in want)
    unwaived_errors = [d for d in reported
                       if d.severity == "error" and d.waiver is None]
    out = {"job": rec.id, "adapter": adapter.name, "status": rec.status,
           "errors": len(unwaived_errors),
           "warnings": sum(1 for d in reported if d.severity == "warning"),
           "waived": wstats["waived_diagnostics"],
           "active_waivers": wstats["active_waivers"],
           "diagnostics": [d.__dict__ for d in reported]}
    if other_errors:
        out["other_files_errors"] = other_errors
        out["note"] = (f"{other_errors} error(s) elsewhere in the design (not in "
                       f"the files you listed) — lint with no paths to see all")
    return truncate(out, max_items=40)


@tool("lint.waive", "DRAFT a lint waiver with justification. Drafts go to "
      ".chipchamp/proposed_waivers.yaml — a human must move them into waivers/ "
      "with status:active + approver before they suppress anything (SPEC §11.4).",
      permission="approve", group="quality",
      schema={"type": "object", "properties": {
          "rule": {"type": "string"}, "scope": {"type": "string"},
          "justification": {"type": "string"}},
          "required": ["rule", "scope", "justification"]})
def lint_waive(ctx: ToolContext, rule: str, scope: str, justification: str) -> dict:
    if len(justification.strip()) < 10:
        return {"error": "justification too thin — explain WHY this is safe to waive"}
    from ..policy.waivers import draft_waiver
    path = draft_waiver(str(ctx.ws.dot), rule=rule, scope=scope,
                        justification=justification)
    return {"status": "proposed", "staged_in": ctx.rel(path), "activated": False,
            "note": "requires human review: move into waivers/ with "
                    "status: active and an approver to take effect"}


@tool("cdc.run", "CDC-lite: report clock-domain crossings from the design DB "
      "(structural, license-free).", cost="cheap", permission="read", group="quality",
      schema={"type": "object", "properties": {"module": {"type": "string"}}})
def cdc_run(ctx: ToolContext, module: str = "") -> dict:
    modules = [module] if module else list(ctx.db.modules)
    crossings = []
    for m in modules:
        dm = ctx.db.domain_report(m)
        if dm:
            for s, a, b, snk in dm.crossings:
                crossings.append({"module": m, "signal": s, "from_clock": a,
                                  "to_clock": b, "sink": snk})
    return truncate({"crossings": crossings, "count": len(crossings),
                     "note": "structural CDC-lite; signoff CDC (Spyglass/Questa) is M2"})


@tool("style.format", "Format sources per project style (verible).", cost="cheap",
      permission="write", group="quality",
      schema={"type": "object", "properties": {
          "paths": {"type": "array", "items": {"type": "string"}}}})
def style_format(ctx: ToolContext, paths=None) -> dict:
    files = _target_files(ctx, paths)
    adapter = ctx.registry.get("verible")
    if not adapter.available():
        return {"error": "verible not available"}
    for f in files:
        rel = ctx.rel(f)
        ok, why = ctx.policy.can_write(rel)
        if not ok:
            return {"error": f"cannot format {rel}: {why}"}
    plan = adapter.format(files, ctx.ws.root)
    rec, res = ctx.submit(plan, adapter, input_files=files, timeout=60)
    return {"job": rec.id, "status": rec.status, "summary": res.summary}


@tool("synth.run", "Synthesis snapshot (Yosys): cell/area estimate + inferred-"
      "latch warnings, as an RTL feedback signal.", cost="metered",
      permission="submit", group="synth",
      schema={"type": "object", "properties": {"top": {"type": "string"}}})
def synth_run(ctx: ToolContext, top: str = "") -> dict:
    adapter = ctx.registry.for_role("synth")
    if not adapter or not adapter.available():
        return {"error": "no synth adapter available"}
    top = top or ctx.target.top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    from ..config import rtl_only
    files = rtl_only(ctx.target.sources)  # never synthesize testbenches
    if not files:
        return {"error": "no synthesizable RTL in the target (only testbench "
                         "files found)"}
    plan = adapter.synth(files, top, ctx.ws.root, incdirs=ctx.target.incdirs,
                         defines=ctx.target.defines)  # +SYNTHESIS (compile out SVA)
    rec, res = ctx.submit(plan, adapter, input_files=files, timeout=180)
    return truncate({"job": rec.id, "status": rec.status, "metrics": res.metrics,
                     "summary": res.summary,
                     "warnings": [d.__dict__ for d in res.warnings][:10]})


@tool("sta.run", "Static timing snapshot. Uses OpenSTA (liberty-timed WNS/TNS) "
      "when installed+configured, else the Yosys longest-topological-path proxy "
      "(logic DEPTH, not delay). Pass baseline_job to compute the delta the "
      "timing gate needs.", cost="metered", permission="submit", group="synth",
      schema={"type": "object", "properties": {
          "top": {"type": "string"},
          "baseline_job": {"type": "string",
                           "description": "earlier sta job to diff against"}}})
def sta_run(ctx: ToolContext, top: str = "", baseline_job: str = "") -> dict:
    top = top or ctx.target.top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    from ..config import rtl_only
    files = rtl_only(ctx.target.sources)  # STA is on the synthesized design
    opensta = ctx.registry.get("opensta")
    tools_cfg = ctx.ws.config.get("tools", {})
    liberty = tools_cfg.get("sta_liberty")
    netlist = tools_cfg.get("sta_netlist")
    if opensta.available() and liberty and netlist:
        plan = opensta.sta(netlist, top, os.path.join(ctx.ws.root, ".chipchamp", "sta"),
                           liberty=liberty, sdc=tools_cfg.get("sta_sdc"))
        adapter = opensta
    else:
        adapter = ctx.registry.get("yosys")
        if not adapter.available():
            return {"error": "no STA adapter available (need opensta+liberty or yosys)"}
        plan = adapter.timing_snapshot(files, top, ctx.ws.root,
                                       incdirs=ctx.target.incdirs,
                                       defines=ctx.target.defines)
    rec, res = ctx.submit(plan, adapter, input_files=files, timeout=240)
    out = {"job": rec.id, "status": rec.status, "mode": res.metrics.get("mode"),
           "metrics": {k: v for k, v in res.metrics.items() if k != "paths"},
           "summary": res.summary}
    if baseline_job:
        base = ctx.runner.get(baseline_job)
        if base and base.kind == "sta":
            delta = _sta_delta(base.result.get("metrics", {}), res.metrics)
            ctx.sta_delta = delta
            out["delta"] = delta
        else:
            out["delta_error"] = f"baseline {baseline_job} is not an sta job"
    return truncate(out)


def _sta_delta(base: dict, cur: dict) -> dict:
    """Timing-gate delta (SPEC §11.1). Liberty mode: new violations count.
    Proxy mode: conservative — any depth increase counts as a violation."""
    if base.get("mode") == "liberty-timed" or cur.get("mode") == "liberty-timed":
        new_viol = max(0, (cur.get("violations") or 0) - (base.get("violations") or 0))
        return {"mode": "liberty-timed", "wns": [base.get("wns"), cur.get("wns")],
                "new_violations": new_viol}
    bd, cd = base.get("depth"), cur.get("depth")
    inc = (cd - bd) if (bd is not None and cd is not None) else None
    return {"mode": "structural-depth", "depth": [bd, cd],
            "new_violations": max(0, inc or 0),
            "note": "proxy: depth increase treated as violation (conservative)"}


@tool("sta.paths", "Worst paths from a completed sta job (normalized records).",
      group="synth",
      schema={"type": "object", "properties": {
          "job": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
          "required": ["job"]})
def sta_paths(ctx: ToolContext, job: str, limit: int = 5) -> dict:
    rec = ctx.runner.get(job)
    if not rec or rec.kind != "sta":
        return {"error": f"{job} is not an sta job"}
    m = rec.result.get("metrics", {})
    if m.get("mode") == "structural-depth":
        return {"job": job, "mode": m["mode"], "depth": m.get("depth"),
                "path": m.get("path", [])[:limit]}
    return truncate({"job": job, "mode": m.get("mode"),
                     "paths": (m.get("paths") or [])[:limit],
                     "wns": m.get("wns"), "tns": m.get("tns")})


@tool("lec.run", "Logic-equivalence check golden vs revised at a module boundary "
      "(the gate that lets an NFC refactor be trusted). Inconclusive != pass.",
      cost="metered", permission="submit", group="lec",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "golden_files": {"type": "array", "items": {"type": "string"}},
          "revised_files": {"type": "array", "items": {"type": "string"}},
          "depth": {"type": "integer", "default": 10}},
          "required": ["module", "golden_files", "revised_files"]})
def lec_run(ctx: ToolContext, module: str, golden_files, revised_files,
            depth: int = 10) -> dict:
    adapter = ctx.registry.for_role("lec")
    if not adapter or not adapter.available():
        return {"error": "no LEC adapter available"}
    gold = _target_files(ctx, golden_files)
    rev = _target_files(ctx, revised_files)
    plan = adapter.lec(gold, rev, module,
                       workdir=os.path.join(ctx.ws.root, ".chipchamp", "lec", module),
                       depth=depth)
    rec, res = ctx.submit(plan, adapter, input_files=gold + rev, timeout=240)
    return {"job": rec.id, "status": rec.status, "verdict": res.status,
            "summary": res.summary,
            "inconclusive_reason": res.metrics.get("inconclusive_reason")}
