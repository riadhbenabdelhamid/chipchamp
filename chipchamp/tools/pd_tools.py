"""Physical-design tools (SPEC §8.4 physical vertical): RTL-to-GDSII + signoff.

``pd.run`` drives the whole LibreLane flow for a module; the query tools read
LibreLane's normalized ``metrics.json`` (timing/DRC/LVS/antenna/area) rather than
the GDS/DEF/DRC databases — "query, don't dump" at the physical scale. Signoff
stays the human's call (SPEC NG2/NG9): the platform reports verdicts + evidence,
it does not authorize a tapeout.
"""
from __future__ import annotations

import os

from ..brand import env_name
from .base import tool, truncate
from .context import ToolContext


def _target_files(ctx: ToolContext) -> list[str]:
    seen, out = set(), []
    for f in ctx.target.sources:
        rp = os.path.realpath(f)
        if rp not in seen:
            seen.add(rp)
            out.append(f)
    return out


def _latest_pnr(ctx: ToolContext):
    for rec in reversed(ctx.task_jobs):
        if rec.kind == "pnr":
            return rec
    for rec in reversed(ctx.runner.list_jobs()):
        if rec.kind == "pnr":
            return rec
    return None


@tool("pd.run", "Run RTL-to-GDSII for a module (LibreLane: synth → floorplan → "
      "place → CTS → route → DRC/LVS/antenna signoff → GDS). Returns the signoff "
      "verdict, key metrics and the GDSII path. Long-running (minutes).",
      cost="metered", permission="submit", group="physical",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "clock_port": {"type": "string", "default": "clk"},
          "clock_period": {"type": "number", "default": 10.0}},
          "required": ["module"]})
def pd_run(ctx: ToolContext, module: str, clock_port: str = "clk",
           clock_period: float = 10.0) -> dict:
    adapter = ctx.registry.for_role("pnr")
    if adapter is None or not adapter.available():
        return {"error": "no RTL-to-GDSII adapter available (need librelane on "
                "PATH or the devshell AppImage)"}
    if not ctx.db.module(module):
        return {"error": f"module '{module}' not in index", "known": sorted(ctx.db.modules)[:30]}
    files = _target_files(ctx)
    workdir = str(ctx.ws.dot / "pd" / module)
    plan = adapter.pnr(files, module, workdir, clock_port=clock_port,
                       clock_period=clock_period)
    rec, res = ctx.submit(plan, adapter, is_v3=True, input_files=files, timeout=3600)
    keep = ("setup_ws_ns", "hold_ws_ns", "drc_violations", "lvs_errors",
            "antenna_violations", "cell_area_um2", "die_area_um2", "utilization",
            "wirelength_um", "power_w")
    return {"job": rec.id, "status": rec.status, "verdict": res.status,
            "gds": ctx.rel(res.artifacts["gds"]) if res.artifacts.get("gds") else None,
            "signoff": {k: res.metrics.get(k) for k in keep},
            "failing_checks": res.metrics.get("failing_checks", []),
            "summary": res.summary}


@tool("pd.metrics", "Normalized physical-signoff metrics from a pnr job "
      "(timing/DRC/LVS/antenna/area/power). Query, don't dump the layout.",
      group="physical",
      schema={"type": "object", "properties": {"job": {"type": "string"}}})
def pd_metrics(ctx: ToolContext, job: str = "") -> dict:
    rec = ctx.runner.get(job) if job else _latest_pnr(ctx)
    if not rec or rec.kind != "pnr":
        return {"error": "no RTL-to-GDSII (pnr) job found"}
    return truncate({"job": rec.id, "verdict": rec.result.get("status"),
                     **rec.result.get("metrics", {})})


@tool("pd.drc", "DRC violation breakdown from a pnr job (Magic/KLayout signoff "
      "+ in-route).", group="physical",
      schema={"type": "object", "properties": {"job": {"type": "string"}}})
def pd_drc(ctx: ToolContext, job: str = "") -> dict:
    rec = ctx.runner.get(job) if job else _latest_pnr(ctx)
    if not rec or rec.kind != "pnr":
        return {"error": "no pnr job found"}
    m = rec.result.get("metrics", {})
    return {"job": rec.id, "magic_drc": m.get("magic_drc"),
            "klayout_drc": m.get("klayout_drc"), "route_drc": m.get("route_drc"),
            "total_signoff_drc": m.get("drc_violations")}


@tool("pd.power", "ACTIVITY-DRIVEN power on the routed netlist: annotate with "
      "a real workload VCD (a sim job's waves), run OpenSTA report_power, and "
      "get power by group (the clock-tree share is the clock-gating signal), "
      "the hottest instances cross-probed to RTL, and the annotation rate so "
      "a bad VCD can't pose as a trustworthy number. Without a VCD it falls "
      "back to vectorless and says so.", cost="metered", permission="submit",
      group="physical",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "activity_from": {"type": "string",
                            "description": "sim job id (uses its waves) or a VCD path"},
          "clock_port": {"type": "string", "default": "clk"},
          "clock_period_ns": {"type": "number", "default": 10.0},
          "corner": {"type": "string", "enum": ["nom", "min", "max"],
                     "default": "nom"},
          "save_baseline": {"type": "boolean", "default": False}}})
def pd_power(ctx: ToolContext, module: str = "", activity_from: str = "",
             clock_port: str = "clk", clock_period_ns: float = 10.0,
             corner: str = "nom", save_baseline: bool = False) -> dict:
    import glob as _glob
    import json as _jsonm

    from ..physical import find_run_dir
    from ..physical.area import find_liberty
    from ..physical.power import (aggregate_by_module, find_dut_scope,
                                  parse_power_report, power_tcl)
    from ..physical.reports import find_synth_netlist
    from ..physical.xprobe import GateNetlist, resolve_hier, rtl_net_of
    module = module or (ctx.db.tops()[0] if ctx.db.tops() else "")
    run_dir = find_run_dir(str(ctx.ws.dot / "pd"), module)
    if not run_dir:
        return {"error": f"no LibreLane run for '{module}' — run pd.run first"}
    nl = _glob.glob(os.path.join(run_dir, "final", "nl", "*.nl.v"))
    spef = _glob.glob(os.path.join(run_dir, "final", "spef", corner, "*.spef"))
    if not nl:
        return {"error": f"no routed netlist under {ctx.rel(run_dir)}/final"}
    liberty = find_liberty()
    if not liberty:
        return {"error": f"no liberty found ({env_name('LIBERTY')} or ciel sky130)"}

    # workload activity: a sim job's waves, or an explicit VCD path
    vcd, scope, activity_src = "", "", "vectorless"
    if activity_from:
        rec_a = ctx.runner.get(activity_from)
        if rec_a and rec_a.artifacts.get("waves"):
            vcd = rec_a.artifacts["waves"]
            activity_src = f"job {activity_from}"
        elif os.path.exists(os.path.join(ctx.ws.root, activity_from)) or \
                os.path.exists(activity_from):
            vcd = activity_from if os.path.isabs(activity_from) \
                else os.path.join(ctx.ws.root, activity_from)
            activity_src = ctx.rel(vcd)
        else:
            return {"error": f"'{activity_from}' is neither a job with waves "
                             f"nor a VCD file"}
    if vcd:
        mod = ctx.db.module(module)
        ports = {p.name for p in mod.ports} if mod else set()
        scope = find_dut_scope(vcd, ports) or ""

    workdir = str(ctx.ws.dot / "power" / module)
    os.makedirs(workdir, exist_ok=True)
    tcl = power_tcl(liberty=liberty, netlist=nl[0], top=module,
                    clock=clock_port, period_ns=clock_period_ns,
                    spef=spef[0] if spef else "", vcd=vcd, scope=scope)
    tcl_path = os.path.join(workdir, f"power.{corner}.tcl")
    with open(tcl_path, "w") as fh:
        fh.write(tcl)
    adapter = ctx.registry.get("librelane")
    if not adapter.available():
        return {"error": "OpenSTA rides in the LibreLane devshell — neither found"}
    plan = adapter.sta_power(tcl_path, workdir)
    rec, res = ctx.submit(plan, adapter, input_files=[nl[0]], timeout=300)
    rep = res.metrics.get("report") or {}
    if not rep.get("total"):
        return {"job": rec.id, "error": "sta produced no power table",
                "summary": res.summary}

    # cross-probe hot instances to RTL through the SYNTHESIS netlist
    syn = find_synth_netlist(run_dir, module)
    netlist = GateNetlist.load(syn) if syn else GateNetlist()
    hot = []
    for i in rep["instances"][:10]:
        entry = dict(i)
        net = rtl_net_of(i["instance"], netlist)
        if net:
            entry["rtl_net"] = net
            r = resolve_hier(ctx.db, module, net)
            if r:
                entry["rtl"] = {k: r[k] for k in ("module", "signal", "file", "line")}
        elif i["instance"].startswith(("clkbuf", "clknet")):
            entry["rtl_net"] = "(clock tree)"
        hot.append(entry)

    out = {"job": rec.id, "module": module, "corner": corner,
           "activity": activity_src,
           "annotation": rep.get("annotation"),
           "total_w": rep["total"]["total_w"],
           "groups": rep["groups"],
           "clock_share_pct": rep.get("clock_share_pct"),
           "hottest": hot,
           "by_module": aggregate_by_module(rep["instances"])}
    base_path = ctx.ws.dot / "power" / f"{module}.baseline.json"
    if base_path.exists():
        base = _jsonm.loads(base_path.read_text())
        d = round(out["total_w"] - base["total_w"], 9)
        out["delta"] = {"from": base["total_w"], "to": out["total_w"],
                        "delta_w": d,
                        **({"better": d < 0} if d else {}),
                        "baseline_job": base.get("_job"),
                        "baseline_activity": base.get("_activity")}
    if save_baseline:
        base_path.write_text(_jsonm.dumps(
            {"total_w": out["total_w"], "clock_share_pct": out["clock_share_pct"],
             "_job": rec.id, "_activity": activity_src}, indent=1))
        out["baseline_saved"] = ctx.rel(str(base_path))
    if activity_src == "vectorless":
        out["warning"] = ("VECTORLESS estimate (default toggle rates) — pass "
                          "activity_from=<sim job> for workload-driven power")
    return truncate(out)


@tool("pd.glsim", "GATE-LEVEL simulation of the routed netlist against an "
      "existing testbench (PDK functional cell models, zero-delay). Two "
      "deliverables: the taped-out netlist passes its RTL self-checks, and "
      "the VCD covers EVERY gate net — pd.power activity_from=<this job> "
      "reaches ~100% annotation instead of boundary-only propagation. "
      "TBs need a `GL ifdef around parameterized DUT instantiation.",
      cost="metered", permission="submit", group="physical",
      schema={"type": "object", "properties": {
          "module": {"type": "string", "description": "pnr'd top module"},
          "test": {"type": "string", "description": "manifest test whose TB to reuse"},
          "seed": {"type": "integer", "default": 1}},
          "required": ["test"]})
def pd_glsim(ctx: ToolContext, test: str, module: str = "", seed: int = 1) -> dict:
    import glob as _glob

    from ..physical import find_run_dir
    from ..physical.power import find_cell_models
    module = module or (ctx.db.tops()[0] if ctx.db.tops() else "")
    run_dir = find_run_dir(str(ctx.ws.dot / "pd"), module)
    if not run_dir:
        return {"error": f"no LibreLane run for '{module}' — run pd.run first"}
    nl = _glob.glob(os.path.join(run_dir, "final", "nl", "*.nl.v"))
    if not nl:
        return {"error": f"no routed netlist under {ctx.rel(run_dir)}/final"}
    models = find_cell_models()
    if not models:
        return {"error": f"no cell simulation models ({env_name('CELL_MODELS')} or "
                         "ciel sky130 verilog/)"}
    t = ctx.find_test(test)
    if not t or t.get("runner", {}).get("kind") not in ("sv", None):
        return {"error": f"'{test}' is not an sv-runner manifest test"}
    runner = t["runner"]
    tb_top = runner.get("tb_top") or f"tb_{test}"
    tb_files = [os.path.join(ctx.ws.root, f) for f in runner.get("files", [])]
    adapter = ctx.registry.get("icarus")
    if not adapter.available():
        return {"error": "icarus (iverilog) not available"}
    workdir = str(ctx.ws.dot / "glsim")
    os.makedirs(workdir, exist_ok=True)
    files = models + nl + tb_files  # NOT the RTL sources: the netlist is the DUT
    plan = adapter.sim(files, tb_top=tb_top, workdir=workdir, seed=seed,
                       waves=True,
                       defines={"FUNCTIONAL": "1", "UNIT_DELAY": "#0",
                                "GL": "1"})
    rec, res = ctx.submit(plan, adapter, seed=seed, input_files=files,
                          timeout=600)
    out = {"job": rec.id, "status": rec.status, "sim_status": res.status,
           "summary": res.summary, "netlist": ctx.rel(nl[0]),
           "gate_level": True}
    if res.artifacts.get("waves"):
        out["waves"] = ctx.rel(res.artifacts["waves"])
        out["next"] = {"pd.power": {"module": module,
                                    "activity_from": rec.id}}
    diags = [d.__dict__ for d in res.diagnostics if d.severity == "error"][:6]
    if diags:
        out["errors"] = diags
    return out


@tool("pd.area", "Per-module standard-cell area attribution: liberty-mapped "
      "yosys synthesis (seconds, no PnR needed) with hierarchy preserved, so "
      "every RTL module gets its own µm² + cell mix, self and rolled-up. The "
      "'where did the area go' primitive.", cost="cheap", permission="submit",
      group="physical",
      schema={"type": "object", "properties": {
          "top": {"type": "string", "description": "root module (default: target top)"},
          "liberty": {"type": "string", "description": "liberty file (default: "
                      f"{env_name('LIBERTY')} or the ciel-managed sky130 HD lib)"},
          "by_line": {"type": "boolean", "default": False,
                      "description": "also attribute area to RTL source lines"}}})
def pd_area(ctx: ToolContext, top: str = "", liberty: str = "",
            by_line: bool = False) -> dict:
    from ..physical.area import (area_plan, demangle, find_liberty,
                                 instance_counts, parse_stat_json, rollup)
    top = top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    if not top:
        return {"error": "no top module"}
    yosys = ctx.registry.get("yosys")
    if not yosys.available():
        return {"error": "yosys not available"}
    lib = liberty or find_liberty()
    files = _target_files(ctx)
    workdir = str(ctx.ws.dot / "ppa")
    os.makedirs(workdir, exist_ok=True)
    out_json = os.path.join(workdir, f"{top}.stat.json")
    out_mapped = os.path.join(workdir, f"{top}.mapped.json")
    plan = area_plan(yosys, files, top, workdir, lib, out_json,
                     out_mapped=out_mapped)
    rec, res = ctx.submit(plan, yosys, input_files=files, timeout=300)
    per_mod = parse_stat_json(out_json)
    if not per_mod:
        return {"job": rec.id, "error": "yosys stat produced no module data",
                "summary": res.summary}
    rolled = rollup(per_mod)
    counts = instance_counts(per_mod, top)
    table = []
    for name, v in rolled.items():
        base, tag = demangle(name)
        n = counts.get(name, 0)
        if n == 0 and name != top:
            continue  # not instantiated under this top
        table.append({
            "module": base + (f" #({tag})" if tag else ""),
            "instances": n or 1,
            "area_per_inst_um2": v["total_area_um2"],
            "design_area_um2": round((n or 1) * v["total_area_um2"], 2),
            "self_area_um2": v["area_um2"],
            "seq_area_um2": v["sequential_area_um2"],
            "cells": v["cells"]})
    table.sort(key=lambda r: -r["design_area_um2"])
    out = {
        "job": rec.id, "top": top,
        "liberty": os.path.basename(lib) if lib else None,
        "mapped": bool(lib),
        "total_area_um2": rolled.get(top, {}).get("total_area_um2"),
        "by_module": table,
        "note": None if lib else "no liberty found — cells counted, area=0; "
                                 f"set {env_name('LIBERTY')} or install a PDK via ciel"}
    if by_line and lib and os.path.exists(out_mapped):
        from ..physical.insights import (area_by_line_multi,
                                         liberty_cell_areas,
                                         load_mapped_modules)
        bl = area_by_line_multi(load_mapped_modules(out_mapped),
                                liberty_cell_areas(lib))
        out["by_line"] = {k: v for k, v in list(bl["lines"].items())[:25]}
        out["by_line_unattributed"] = bl["unattributed"]
        out["by_line_note"] = ("flops carry exact source refs; combinational "
                               "cells are attributed to the nearest source-"
                               "carrying cell downstream (their endpoint "
                               "register's line)")
    return truncate(out)


@tool("pd.deadwidth", "Dead-width findings: cross per-bit TOGGLE COVERAGE "
      "(which bits never moved across the workload) with the mapped netlist + "
      "liberty (what each bit's flop costs). Surfaces oversized counters/"
      "buses/state vectors — waste no single tool sees. Needs a coverage set "
      "(cov.run/cov.load) and a pd.area run (for the mapped netlist).",
      group="physical",
      schema={"type": "object", "properties": {
          "top": {"type": "string"},
          "set": {"type": "string", "default": "default"},
          "min_width": {"type": "integer", "default": 2}}})
def pd_deadwidth(ctx: ToolContext, top: str = "", set: str = "default",
                 min_width: int = 2) -> dict:
    from ..physical.area import find_liberty
    from ..physical.insights import (dead_bits, liberty_cell_areas,
                                     load_mapped_modules)
    top = top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    svc = ctx.coverage_sets.get(set) or ctx.ws.coverage_baseline()
    if not svc:
        return {"error": f"no coverage set '{set}' and no baseline — run "
                         f"cov.run first (toggle coverage feeds this)"}
    n_toggle = sum(1 for b in svc.model.bins.values() if b.kind == "toggle")
    if not n_toggle:
        return {"error": "coverage has no toggle bins (need verilator "
                         "coverage: cov.run)"}
    mapped = ctx.ws.dot / "ppa" / f"{top}.mapped.json"
    cells, areas = None, None
    lib = find_liberty()
    if mapped.exists() and lib:
        # flops across EVERY module — coverage signals live in submodules
        cells = [c for mod_cells in load_mapped_modules(str(mapped)).values()
                 for c in mod_cells]
        areas = liberty_cell_areas(lib)
    findings = [f for f in dead_bits(svc.model, cells, areas)
                if f["width"] >= min_width]
    wasted = round(sum(f["wasted_area_um2"] for f in findings), 2)
    return truncate({
        "top": top, "coverage_set": set, "toggle_bins": n_toggle,
        "findings": findings[:20], "total_findings": len(findings),
        "wasted_area_um2": wasted,
        "priced": bool(cells),
        "note": None if cells else "no mapped netlist (run pd.area first) — "
                                   "findings unpriced but still real"})


@tool("pd.ppa", "One PPA picture for a module: post-route timing/area/power/"
      "signoff from the pnr job + per-module synthesis area. save_baseline "
      "stores it; later calls report signed deltas — the RTL-edit feedback "
      "loop (did that change cost area? slack? power?).", group="physical",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "job": {"type": "string", "description": "pnr job (default: latest)"},
          "save_baseline": {"type": "boolean", "default": False},
          "include_area_breakdown": {"type": "boolean", "default": False}}})
def pd_ppa(ctx: ToolContext, module: str = "", job: str = "",
           save_baseline: bool = False, include_area_breakdown: bool = False) -> dict:
    import json as _json

    from ..physical.area import ppa_delta, ppa_snapshot
    rec = ctx.runner.get(job) if job else _latest_pnr(ctx)
    if not rec or rec.kind != "pnr":
        return {"error": "no RTL-to-GDSII (pnr) job found — run pd.run first"}
    if not module:
        # the job's artifacts live under .../pd/<module>/...
        gds = rec.artifacts.get("gds", "") or rec.result.get("artifacts", {}).get("gds", "")
        parts = gds.replace("\\", "/").split("/")
        module = parts[parts.index("pd") + 1] if "pd" in parts else \
            (ctx.db.tops()[0] if ctx.db.tops() else "")
    metrics = rec.result.get("metrics", {})
    area_by_module = None
    if include_area_breakdown:
        area = pd_area(ctx, top=module)
        if "by_module" in area:
            area_by_module = {r["module"]: r for r in area["by_module"]}
    snap = ppa_snapshot(metrics, area_by_module)
    base_path = ctx.ws.dot / "ppa" / f"{module}.baseline.json"
    out = {"module": module, "job": rec.id, "current": snap}
    if base_path.exists():
        baseline = _json.loads(base_path.read_text())
        out["baseline_job"] = baseline.get("_job")
        out["delta"] = ppa_delta(snap, baseline)
        regress = [k for k, v in out["delta"].items()
                   if isinstance(v, dict) and v.get("better") is False]
        out["regressions"] = regress
    if save_baseline:
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(_json.dumps({**snap, "_job": rec.id}, indent=1))
        out["baseline_saved"] = str(ctx.rel(str(base_path)))
    return truncate(out)


@tool("pd.sweep", "Sweep synthesis strategies and return a PARETO TABLE — "
      "area (µm²) vs worst-path delay (ns, real OpenSTA) per config, dominated "
      "configs marked. mode=synth (seconds: ABC mapping-script knob, measured "
      ">50% area swing on comb-heavy logic) or mode=pnr (minutes/config: full "
      "LibreLane SYNTH_STRATEGY runs compared post-route). The 'which recipe "
      "should this block use' decision, made with data.",
      cost="metered", permission="submit", group="physical",
      schema={"type": "object", "properties": {
          "top": {"type": "string"},
          "mode": {"type": "string", "enum": ["synth", "pnr"], "default": "synth"},
          "clock_port": {"type": "string", "default": "clk"},
          "clock_period_ns": {"type": "number", "default": 10.0},
          "strategies": {"type": "array", "items": {"type": "string"},
                         "description": "pnr mode: SYNTH_STRATEGY values "
                                        "(default: AREA 0/2, DELAY 0/2)"}}})
def pd_sweep(ctx: ToolContext, top: str = "", mode: str = "synth",
             clock_port: str = "clk", clock_period_ns: float = 10.0,
             strategies=None) -> dict:
    from ..physical.area import find_liberty, parse_stat_json
    from ..physical.reports import parse_path_report
    from ..physical.sweep import (PNR_STRATEGIES, SYNTH_CONFIGS, delay_tcl,
                                  pareto, synth_config_plan)
    top = top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    if not top:
        return {"error": "no top module"}
    files = _target_files(ctx)
    workdir = str(ctx.ws.dot / "sweep" / top)
    os.makedirs(workdir, exist_ok=True)

    if mode == "pnr":
        adapter = ctx.registry.for_role("pnr")
        if adapter is None or not adapter.available():
            return {"error": "no RTL-to-GDSII adapter for pnr-mode sweep"}
        rows = []
        for strat in (strategies or PNR_STRATEGIES):
            tag = "sweep-" + strat.lower().replace(" ", "")
            plan = adapter.pnr(files, top, str(ctx.ws.dot / "pd" / top),
                               clock_port=clock_port,
                               clock_period=clock_period_ns, run_tag=tag,
                               extra={"SYNTH_STRATEGY": strat})
            rec, res = ctx.submit(plan, adapter, is_v3=True,
                                  input_files=files, timeout=3600)
            rows.append({"config": strat, "job": rec.id,
                         "area_um2": res.metrics.get("cell_area_um2"),
                         "slack_ns": res.metrics.get("setup_ws_ns"),
                         "power_w": res.metrics.get("power_w"),
                         "verdict": res.status})
        pareto(rows, {"area_um2": "min", "slack_ns": "max"})
        return truncate({"top": top, "mode": "pnr", "table": rows,
                         "pareto": [r["config"] for r in rows if r["pareto"]]})

    yosys = ctx.registry.get("yosys")
    if not yosys.available():
        return {"error": "yosys not available"}
    lib = find_liberty()
    if not lib:
        return {"error": f"no liberty found ({env_name('LIBERTY')} or ciel sky130)"}
    devshell = ctx.registry.get("librelane")
    rows = []
    for name, abc_tail in SYNTH_CONFIGS.items():
        stat = os.path.join(workdir, f"{name}.stat.json")
        nl = os.path.join(workdir, f"{name}.v")
        plan = synth_config_plan(yosys, files, top, workdir, lib, name,
                                 abc_tail, stat, nl)
        rec, _ = ctx.submit(plan, yosys, input_files=files, timeout=300)
        per = parse_stat_json(stat)
        # total design area for this config = sum of module self-areas
        area = round(sum(m["area_um2"] for m in per.values()), 2) if per else None
        row = {"config": name, "job": rec.id, "area_um2": area,
               "delay_ns": None, "cells": sum(m["cells"] for m in per.values())
               if per else None}
        if devshell.available() and os.path.exists(nl):
            tcl_path = os.path.join(workdir, f"{name}.delay.tcl")
            with open(tcl_path, "w") as fh:
                fh.write(delay_tcl(lib, nl, top, clock_port, clock_period_ns))
            sres = devshell._run_devshell(
                f"sta -no_init -exit {tcl_path}", timeout=180)
            paths = parse_path_report(sres.stdout)
            if paths:
                row["delay_ns"] = round(paths[0].arrival, 3)
                row["slack_ns"] = round(paths[0].slack, 3)
        rows.append(row)
    pareto(rows, {"area_um2": "min", "delay_ns": "min"})
    note = None
    if all(r["delay_ns"] is None for r in rows):
        note = "no OpenSTA (LibreLane devshell) — area-only sweep"
    return truncate({"top": top, "mode": "synth",
                     "clock_period_ns": clock_period_ns, "table": rows,
                     "pareto": [r["config"] for r in rows if r["pareto"]],
                     "note": note})


@tool("pd.critical", "Worst post-route timing paths CROSS-PROBED back to RTL: "
      "each path's start/endpoint is mapped through the gate netlist to the "
      "RTL signal + module + file:line, with its fan-in cone and the stages "
      "where the delay went. The physical→RTL feedback primitive.",
      group="physical",
      schema={"type": "object", "properties": {
          "module": {"type": "string", "description": "pnr'd top module"},
          "corner": {"type": "string", "default": "nom_*",
                     "description": "corner glob, e.g. nom_* or max_ss_*"},
          "path_type": {"type": "string", "enum": ["max", "min"], "default": "max"},
          "n": {"type": "integer", "default": 3},
          "run": {"type": "string", "description": "run name (default: newest)"}}})
def pd_critical(ctx: ToolContext, module: str = "", corner: str = "nom_*",
                path_type: str = "max", n: int = 3, run: str = "") -> dict:
    from ..physical import (GateNetlist, find_run_dir, find_sta_reports,
                            parse_path_report, resolve_hier, rtl_net_of)
    from ..physical.reports import find_synth_netlist
    module = module or (ctx.db.tops()[0] if ctx.db.tops() else "")
    run_dir = find_run_dir(str(ctx.ws.dot / "pd"), module, run=run)
    if not run_dir:
        return {"error": f"no LibreLane run found for '{module}' — run pd.run first"}
    reports = find_sta_reports(run_dir, corner_glob=corner)
    if not reports:
        return {"error": f"no post-PnR STA reports under {ctx.rel(run_dir)} "
                         f"for corner glob '{corner}'"}
    nl_path = find_synth_netlist(run_dir, module)
    netlist = GateNetlist.load(nl_path) if nl_path else GateNetlist()

    paths = []
    for cname, rpts in reports.items():
        if path_type in rpts:
            with open(rpts[path_type], "r", errors="replace") as fh:
                paths += parse_path_report(fh.read(), corner=cname)
    if not paths:
        return {"error": f"no '{path_type}' paths parsed from {sorted(reports)}"}
    paths.sort(key=lambda p: p.slack)

    def probe(point: str, kind: str, end: str) -> dict:
        d: dict = {"point": point, "kind": kind}
        net = rtl_net_of(point, netlist)
        if net:
            d["rtl_net"] = net
            rtl = resolve_hier(ctx.db, module, net)
            if rtl:
                d["rtl"] = rtl
                # endpoint: what drives it (fan-in); startpoint: what it feeds
                direction = "fanin" if end == "endpoint" else "fanout"
                cone = ctx.db.cone(rtl["module"], rtl["signal"], direction, 2)
                if cone and cone.statements:
                    d["cone"] = cone.statements[:6]
        return d

    out_paths = []
    for p in paths[:n]:
        out_paths.append({
            "corner": p.corner, "slack_ns": p.slack,
            "met": p.met, "group": p.group,
            "startpoint": probe(p.startpoint, p.startpoint_kind, "startpoint"),
            "endpoint": probe(p.endpoint, p.endpoint_kind, "endpoint"),
            "worst_stages": p.worst_stages(3),
            "stages": len(p.points),
        })
    return truncate({
        "module": module, "path_type": path_type,
        "run": ctx.rel(run_dir), "corners": sorted(reports),
        "wns_ns": paths[0].slack, "violated": sum(1 for p in paths if not p.met),
        "paths": out_paths,
        "note": "rtl_net recovered from the synthesis netlist (names stable "
                "through PnR); edit the cone, re-run pd.run, compare pd.ppa"})
