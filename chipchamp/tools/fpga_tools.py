"""FPGA design-checkpoint tools (SPEC §8.4 FPGA vertical).

``fpga.run`` implements a module through the flow and leaves a *checkpoint*
per stage — Vivado ``.dcp`` (post_synth/post_place/post_route) or the open
flow's yosys/nextpnr JSON pair. ``fpga.checkpoints`` lists and compares them,
``fpga.ppa`` is the baseline/delta feedback loop (fmax/WNS/LUT/FF/power), and
``fpga.critical`` cross-probes the worst post-route path back to RTL
file:line — same discipline as the ASIC side, at FPGA iteration speed.
"""
from __future__ import annotations

import json as _json
import os
import shutil

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


def _latest_fpga(ctx: ToolContext, module: str = ""):
    for rec in reversed(ctx.task_jobs + ctx.runner.list_jobs()):
        if rec.kind == "fpga":
            if module and f"/fpga/{module}/" not in \
                    (rec.artifacts.get("report") or
                     rec.artifacts.get("post_route_dcp") or
                     next(iter(rec.artifacts.values()), "")):
                continue
            return rec
    return None


def _workdir(ctx: ToolContext, module: str, flow: str) -> str:
    d = str(ctx.ws.dot / "fpga" / module / flow)
    os.makedirs(d, exist_ok=True)
    return d


def _stage_snapshots(rec) -> dict[str, dict]:
    """Normalized per-stage checkpoint snapshots from an fpga job record."""
    from ..physical.fpga import checkpoint_snapshot, parse_nextpnr_report
    m = rec.result.get("metrics", {})
    snaps: dict[str, dict] = {}
    if m.get("flow") == "vivado":
        for st in m.get("stages", []):
            timing = {k.split(".", 1)[1]: v for k, v in m.items()
                      if k.startswith(f"{st}.") and not k.split(".", 1)[1]
                      .endswith(("_used", "_avail"))}
            util = {}
            for k, v in m.items():
                if k.startswith(f"{st}.") and k.endswith("_used"):
                    res = k.split(".", 1)[1][:-5]
                    util[res] = {"used": v,
                                 "available": m.get(f"{st}.{res}_avail", 0)}
            power = ({"power_w": m["post_route.power_w"]}
                     if st == "post_route" and "post_route.power_w" in m else None)
            snaps[st] = checkpoint_snapshot(st, timing=timing,
                                            utilization=util, power=power)
    else:  # nextpnr open flow
        rep_path = rec.artifacts.get("report", "")
        rep = parse_nextpnr_report(rep_path) if os.path.exists(rep_path) else {}
        if "post_synth" in m.get("stages", []):
            snaps["post_synth"] = checkpoint_snapshot("post_synth")
        if rep:
            snaps["post_route"] = checkpoint_snapshot(
                "post_route", fmax=rep.get("fmax"),
                utilization=rep.get("utilization"))
    return snaps


@tool("fpga.run", "Implement a module for an FPGA and write a design "
      "checkpoint per stage. Open flow (yosys+nextpnr: post_synth/post_route "
      "JSON + report) or Vivado (.dcp at post_synth/post_place/post_route "
      "with timing/utilization/power reports). Returns the timing verdict "
      "and checkpoint inventory.", cost="metered", permission="submit",
      group="fpga",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "flow": {"type": "string", "enum": ["nextpnr", "vivado"],
                   "description": "default: first available (nextpnr)"},
          "family": {"type": "string",
                     "description": "nextpnr family: ice40|ecp5|machxo2|nexus"},
          "part": {"type": "string", "description": "vivado part, e.g. xc7a35tcpg236-1"},
          "freq_mhz": {"type": "number", "default": 100.0},
          "clock_port": {"type": "string", "default": "clk"},
          "ooc": {"type": "boolean", "default": True,
                  "description": "vivado: out-of-context (module-only). Set "
                                 "false for a full implementation — required "
                                 "before a bitstream can be written."},
          "xdc": {"type": "string",
                  "description": "vivado: constraints file (pin LOC + "
                                 "IOSTANDARD). Required for a bitstream — "
                                 "without it Vivado's UCIO-1/NSTD-1 DRCs "
                                 "block write_bitstream."}},
          "required": ["module"]})
def fpga_run(ctx: ToolContext, module: str, flow: str = "", family: str = "ice40",
             part: str = "xc7a35tcpg236-1", freq_mhz: float = 100.0,
             clock_port: str = "clk", ooc: bool = True, xdc: str = "") -> dict:
    if not ctx.db.module(module):
        return {"error": f"module '{module}' not in index",
                "known": sorted(ctx.db.modules)[:30]}
    if not flow:
        flow = "nextpnr" if ctx.registry.get("nextpnr").available() else "vivado"
    adapter = ctx.registry.get(flow)
    if not adapter.available():
        return {"error": f"{flow} not available on this machine",
                "hint": "caps shows the fpga role's live backends"}
    files = _target_files(ctx)
    workdir = _workdir(ctx, module, flow)
    if flow == "vivado":
        xdc_path = ""
        if xdc:
            xdc_path = xdc if os.path.isabs(xdc) else os.path.join(ctx.ws.root, xdc)
            if not os.path.isfile(xdc_path):
                return {"error": f"constraints file not found: {ctx.rel(xdc_path)}"}
        plan = adapter.fpga_flow(files, module, workdir, part=part,
                                 clock_port=clock_port,
                                 clock_period_ns=round(1000.0 / freq_mhz, 3),
                                 out_of_context=ooc, xdc=xdc_path)
        timeout = 1800
    else:
        plan = adapter.fpga_flow(files, module, workdir, family=family,
                                 freq_mhz=freq_mhz)
        timeout = 600
    rec, res = ctx.submit(plan, adapter, is_v3=True, input_files=files,
                          timeout=timeout)
    ckpts = {k: ctx.rel(v) for k, v in res.artifacts.items()
             if k != "tcl" and os.path.exists(v)}
    return truncate({
        "job": rec.id, "status": rec.status, "verdict": res.status,
        "flow": flow, "summary": res.summary,
        "stages": res.metrics.get("stages", []),
        "checkpoints": ckpts,
        "metrics": {k: v for k, v in res.metrics.items()
                    if not k.startswith(("post_synth.", "post_place."))},
        "next": {"fpga.ppa": "baseline/delta", "fpga.critical": "worst path → RTL"}})


@tool("fpga.checkpoints", "List the design checkpoints of the latest FPGA run "
      "(stage, file, normalized metrics) and compare two stages — e.g. how "
      "much timing moved between post_place and post_route.", group="fpga",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "job": {"type": "string"},
          "compare": {"type": "array", "items": {"type": "string"},
                      "description": "two stage names to diff"}}})
def fpga_checkpoints(ctx: ToolContext, module: str = "", job: str = "",
                     compare=None) -> dict:
    from ..physical.fpga import checkpoint_delta
    rec = ctx.runner.get(job) if job else _latest_fpga(ctx, module)
    if not rec or rec.kind != "fpga":
        return {"error": "no FPGA job found — run fpga.run first"}
    snaps = _stage_snapshots(rec)
    files = {k: ctx.rel(v) for k, v in rec.artifacts.items()
             if os.path.exists(v) and ("dcp" in k or k in
                                       ("post_synth", "post_route", "report"))}
    out = {"job": rec.id, "flow": rec.result.get("metrics", {}).get("flow", "nextpnr"),
           "stages": list(snaps), "files": files, "snapshots": snaps}
    if compare and len(compare) == 2:
        a, b = compare
        if a in snaps and b in snaps:
            out["compare"] = {f"{a}→{b}": checkpoint_delta(snaps[b], snaps[a])}
        else:
            out["compare_error"] = f"need two of {list(snaps)}"
    return truncate(out)


@tool("fpga.ppa", "FPGA PPA picture from the post_route checkpoint (fmax/WNS, "
      "LUT/FF/BRAM/DSP, power) with save_baseline → later calls report signed "
      "deltas annotated better/worse. The 'what did that RTL edit cost' loop "
      "at FPGA speed.", group="fpga",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "job": {"type": "string"},
          "save_baseline": {"type": "boolean", "default": False}}})
def fpga_ppa(ctx: ToolContext, module: str = "", job: str = "",
             save_baseline: bool = False) -> dict:
    from ..physical.fpga import checkpoint_delta
    rec = ctx.runner.get(job) if job else _latest_fpga(ctx, module)
    if not rec or rec.kind != "fpga":
        return {"error": "no FPGA job found — run fpga.run first"}
    snaps = _stage_snapshots(rec)
    cur = snaps.get("post_route")
    if not cur:
        return {"error": f"no post_route checkpoint in {rec.id} "
                         f"(stages: {list(snaps)})", "job": rec.id}
    if not module:
        art = next(iter(rec.artifacts.values()), "")
        parts = art.replace("\\", "/").split("/")
        module = parts[parts.index("fpga") + 1] if "fpga" in parts else "design"
    flow = rec.result.get("metrics", {}).get("flow", "nextpnr")
    base_path = ctx.ws.dot / "fpga" / f"{module}.{flow}.baseline.json"
    out = {"module": module, "flow": flow, "job": rec.id, "current": cur}
    if base_path.exists():
        baseline = _json.loads(base_path.read_text())
        out["baseline_job"] = baseline.pop("_job", None)
        out["delta"] = checkpoint_delta(cur, baseline)
        out["regressions"] = [k for k, v in out["delta"].items()
                              if isinstance(v, dict) and v.get("better") is False]
    if save_baseline:
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(_json.dumps({**cur, "_job": rec.id}, indent=1))
        out["baseline_saved"] = ctx.rel(str(base_path))
    return truncate(out)


@tool("fpga.critical", "Worst post-route FPGA path(s) cross-probed to RTL. "
      "Open flow: nextpnr's report carries native RTL source refs per segment. "
      "Vivado: endpoint registers (count_q_reg[3]/C) resolve through the "
      "design DB to module + file:line + fan-in cone, with the logic/route "
      "split and LUT levels.", group="fpga",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "job": {"type": "string"},
          "n": {"type": "integer", "default": 3}}})
def fpga_critical(ctx: ToolContext, module: str = "", job: str = "",
                  n: int = 3) -> dict:
    from ..physical.fpga import (parse_nextpnr_report, parse_vivado_paths,
                                 rtl_sources_of_path, vivado_rtl_name)
    from ..physical.xprobe import resolve_hier
    rec = ctx.runner.get(job) if job else _latest_fpga(ctx, module)
    if not rec or rec.kind != "fpga":
        return {"error": "no FPGA job found — run fpga.run first"}
    m = rec.result.get("metrics", {})
    top = module or m.get("top", "") or \
        (ctx.db.tops()[0] if ctx.db.tops() else "")

    def probe(rtl_name: str, direction: str) -> dict | None:
        r = resolve_hier(ctx.db, top, rtl_name) if rtl_name else None
        if not r:
            return None
        cone = ctx.db.cone(r["module"], r["signal"], direction, 2)
        if cone and cone.statements:
            r["cone"] = cone.statements[:5]
        return r

    if m.get("flow") == "vivado":
        rpt = rec.artifacts.get("post_route_paths", "")
        if not os.path.exists(rpt):
            return {"error": "no post_route paths report in the job"}
        with open(rpt, "r", errors="replace") as fh:
            paths = parse_vivado_paths(fh.read())
        paths.sort(key=lambda p: p["slack_ns"])
        out_paths = []
        for p in paths[:n]:
            src = vivado_rtl_name(p.get("source", ""))
            dst = vivado_rtl_name(p.get("destination", ""))
            out_paths.append({
                "slack_ns": p["slack_ns"], "met": p["met"],
                "source": p.get("source"), "destination": p.get("destination"),
                "datapath_ns": p.get("datapath_ns"),
                "logic_ns": p.get("logic_ns"), "route_ns": p.get("route_ns"),
                "logic_levels": p.get("logic_levels"),
                "level_mix": p.get("level_mix"),
                "source_rtl": probe(src, "fanout"),
                "destination_rtl": probe(dst, "fanin")})
        return truncate({"flow": "vivado", "module": top, "job": rec.id,
                         "wns_ns": paths[0]["slack_ns"] if paths else None,
                         "paths": out_paths})

    rep_path = rec.artifacts.get("report", "")
    rep = parse_nextpnr_report(rep_path) if os.path.exists(rep_path) else {}
    if not rep.get("critical_paths"):
        return {"error": "no critical paths in the nextpnr report"}
    out_paths = []
    for p in rep["critical_paths"][:n]:
        heavy = rtl_sources_of_path(p)
        # enrich the heaviest segment's net with a design-DB cone
        for h in heavy:
            net = (h.get("net") or "").split("$")[0]
            if net:
                r = probe(net, "fanin")
                if r:
                    h["resolved"] = r
                    break
        out_paths.append({"clock": p["clock"], "delay_ns": p["delay_ns"],
                          "segments": len(p["segments"]),
                          "heaviest_rtl": heavy})
    wf = rep.get("fmax", {})
    return truncate({"flow": "nextpnr", "module": top, "job": rec.id,
                     "fmax": wf, "paths": out_paths,
                     "note": "heaviest_rtl[].rtl are native file:line refs "
                             "from yosys src attributes through P&R"})


def _fpga_flow_of(rec) -> str:
    return (rec.result.get("metrics", {}) or {}).get("flow", "nextpnr")


@tool("fpga.bitstream",
      "Pack the latest FPGA run into a FLASHABLE bitstream (icepack/ecppack/"
      "prjoxide, or Vivado write_bitstream). P&R alone stops at a routed "
      "netlist; this produces the file you actually program. Returns its path "
      "and size.", cost="metered", permission="submit", group="fpga",
      schema={"type": "object", "properties": {
          "module": {"type": "string", "description": "defaults to the latest run"},
          "job": {"type": "string", "description": "pack a specific fpga job"}}})
def fpga_bitstream(ctx: ToolContext, module: str = "", job: str = "") -> dict:
    rec = ctx.runner.get(job) if job else _latest_fpga(ctx, module)
    if rec is None or rec.kind != "fpga":
        return {"error": "no FPGA implementation job found — run fpga.run first"}
    metrics = rec.result.get("metrics", {}) or {}
    flow = _fpga_flow_of(rec)
    if "post_route" not in (metrics.get("stages") or []):
        return {"error": f"job {rec.id} has no post_route checkpoint — "
                         "P&R must complete before a bitstream can be packed",
                "stages": metrics.get("stages", [])}
    adapter = ctx.registry.get(flow)
    if adapter is None or not adapter.available():
        return {"error": f"{flow} not available on this machine"}
    top = metrics.get("top", "") or module
    family = metrics.get("family", "")
    if flow == "vivado":
        # OOC implements the module alone: no IO buffers, no pin constraints —
        # Vivado cannot write a bitstream from it. Say so before burning a run.
        # Only an EXPLICIT out-of-context record blocks: a job predating this
        # metric is unknown, and letting Vivado rule on it beats false blocking.
        if metrics.get("out_of_context") is True:
            return {"error": "the latest Vivado run was out-of-context, which "
                             "cannot produce a bitstream",
                    "hint": "re-run `fpga.run` with ooc=false (a full "
                            "implementation with pin constraints), then pack"}
        config = rec.artifacts.get("post_route_dcp", "")
    else:
        config = rec.artifacts.get("bitstream_config", "")
        if not config:
            return {"error": f"job {rec.id} predates bitstream support "
                             "(no packer input was written) — re-run fpga.run",
                    "hint": "fpga.run now always writes the packer's input"}
        packer = adapter.packer_for(family)
        if packer and not shutil.which(packer):
            return {"error": f"{packer} not on PATH — needed to pack a "
                             f"{family} bitstream",
                    "hint": "it ships with oss-cad-suite"}
    if not config or not os.path.exists(config):
        return {"error": f"packer input missing: {ctx.rel(config or '(none)')}"}
    workdir = _workdir(ctx, top or "design", flow)
    plan = adapter.pack_flow(config, family, workdir, top=top)
    rec2, res = ctx.submit(plan, adapter, is_v3=True, input_files=[], timeout=900)
    bit = res.artifacts.get("bitstream", "")
    out = {
        "job": rec2.id, "status": rec2.status, "verdict": res.status,
        "from_job": rec.id, "flow": flow, "family": family or None,
        "packer": res.metrics.get("packer"),
        "bitstream": ctx.rel(bit) if bit else None,
        "bitstream_bytes": res.metrics.get("bitstream_bytes"),
        "summary": res.summary}
    if res.metrics.get("needs_pin_constraints"):
        out["hint"] = ("every top-level port needs a pin LOC and an IOSTANDARD. "
                       "Write an .xdc for your board and re-run "
                       "`fpga.run ooc=false xdc=<file>`, then pack again.")
    return truncate(out)


@tool("fpga.pins",
      "Pin constraints for a BOARD: validate an .xdc/.pcf/.lpf against the "
      "design's real top-level ports (typos, unconstrained ports, missing "
      "IOSTANDARD, duplicate pins) in a second instead of after a multi-minute "
      "run, or generate one for a known board. `action`: validate | generate | "
      "boards | import.", cost="cheap", permission="read", group="fpga",
      schema={"type": "object", "properties": {
          "action": {"type": "string",
                     "enum": ["validate", "generate", "boards", "import"],
                     "default": "validate"},
          "module": {"type": "string", "description": "top module"},
          "file": {"type": "string",
                   "description": "constraints file to validate/import, or "
                                  "where to write when generating"},
          "board": {"type": "string", "description": "board name (see action=boards)"},
          "format": {"type": "string", "enum": ["xdc", "pcf", "lpf"],
                     "description": "default: from the file extension/board"},
          "map": {"type": "object",
                  "description": "port -> board signal, e.g. {\"count\": "
                                 "\"led_4bits_tri_o\"}; bus bases allowed"},
          "write": {"type": "boolean", "default": False,
                    "description": "generate: write `file` instead of returning text"}}})
def fpga_pins(ctx: ToolContext, action: str = "validate", module: str = "",
              file: str = "", board: str = "", format: str = "",
              map: dict | None = None, write: bool = False) -> dict:
    from ..physical.pins import (board_from_constraints, boards_dir,
                                 detect_format, discover_boards,
                                 emit_constraints, expand_ports, map_ports,
                                 parse_constraints, validate)
    known = discover_boards(ctx.ws)
    if action == "boards":
        return truncate({
            "boards": [{"name": b.name, "part": b.part, "format": b.fmt,
                        "signals": len(b.signals), "source": b.source}
                       for b in sorted(known.values(), key=lambda x: x.name)],
            "note": "board profiles come from Vivado's installed board files "
                    "and .chipchamp/boards/*.json — add one with action=import"})

    path = ""
    if file:
        path = file if os.path.isabs(file) else os.path.join(ctx.ws.root, file)
    fmt = format or (detect_format(path) if path else "xdc")

    if action == "import":
        if not path or not os.path.isfile(path):
            return {"error": f"constraints file not found: {file or '(none)'}"}
        name = board or os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", errors="replace") as fh:
            b = board_from_constraints(name, fh.read(), fmt)
        if not b.signals:
            return {"error": f"no pin assignments parsed from {ctx.rel(path)}"}
        os.makedirs(boards_dir(ctx.ws), exist_ok=True)
        dest = os.path.join(boards_dir(ctx.ws), f"{name}.json")
        with open(dest, "w") as fh:
            _json.dump(b.to_json(), fh, indent=2)
        return {"imported": name, "signals": len(b.signals),
                "profile": ctx.rel(dest),
                "note": "now usable as board=" + name}

    mod = ctx.db.module(module) if module else None
    if mod is None:
        return {"error": f"module '{module}' not in index" if module
                else "give module=<top>",
                "known": sorted(ctx.db.modules)[:30]}
    bits = expand_ports(mod)

    if action == "generate":
        b = known.get(board)
        if b is None:
            return {"error": f"unknown board '{board}'" if board
                    else "give board=<name> (action=boards lists them)",
                    "known": sorted(known)[:40]}
        rows, unmapped = map_ports(bits, b, map)
        text = emit_constraints(
            rows, format or b.fmt, todo=unmapped,
            header=(f"{b.name} ({b.part}) — generated by chipchamp for "
                    f"{mod.name}\nPins come from the {b.source} board profile; "
                    f"unmapped ports are listed below, never guessed.\n"
                    f"Verify against your board's reference manual before "
                    f"programming hardware."))
        out = {"board": b.name, "part": b.part, "module": mod.name,
               "format": format or b.fmt, "mapped": len(rows),
               "unmapped": unmapped,
               "note": "unmapped ports are emitted as TODO comments — map them "
                       "with map={\"port\": \"board_signal\"}"}
        if write and path:
            with open(path, "w") as fh:
                fh.write(text)
            out["written"] = ctx.rel(path)
        else:
            out["constraints"] = text
        return truncate(out)

    # validate
    if not path or not os.path.isfile(path):
        return {"error": f"constraints file not found: {file or '(none)'}"}
    with open(path, "r", errors="replace") as fh:
        parsed, unparsed = parse_constraints(fh.read(), fmt)
    findings = validate(bits, parsed, board=known.get(board), fmt=fmt,
                        unparsed=unparsed)
    errors = [f for f in findings if f["severity"] == "error"]
    return truncate({
        "file": ctx.rel(path), "format": fmt, "module": mod.name,
        "ports": len(bits), "constrained": len(parsed),
        "ok": not errors, "errors": len(errors),
        "warnings": len(findings) - len(errors),
        "findings": findings,
        "note": ("all top-level ports are constrained" if not findings
                 else "fix these before the flow — they otherwise surface "
                      "after synthesis or at write_bitstream")})
