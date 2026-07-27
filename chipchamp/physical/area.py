"""Per-module area attribution (yosys ``stat -liberty``) + PPA snapshotting.

Fast feedback: a liberty-mapped synthesis of the whole target takes seconds and
attributes standard-cell area to every RTL module — no PnR run needed. The PPA
snapshot/delta machinery persists the numbers so an RTL edit can answer "what
did that cost" against the previous run (timing gate's sibling for area/power).
"""
from __future__ import annotations

import glob
import json
import os
import re

from ..adapters.base import Plan, Step

_LIBERTY_GLOBS = (
    # ciel-managed sky130 (LibreLane's PDK layout)
    "~/.ciel/ciel/sky130/versions/*/sky130A/libs.ref/sky130_fd_sc_hd/lib/"
    "sky130_fd_sc_hd__tt_025C_1v80.lib",
    "~/.ciel/ciel/sky130/versions/*/sky130B/libs.ref/sky130_fd_sc_hd/lib/"
    "sky130_fd_sc_hd__tt_025C_1v80.lib",
    "~/.volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
)


def find_liberty() -> str | None:
    """A typical-corner liberty for area mapping: $CHIPCHAMP_LIBERTY first,
    else the ciel/volare-managed sky130 HD library."""
    from ..brand import env as benv
    env = benv("LIBERTY", "")
    if env and os.path.exists(env):
        return env
    for pat in _LIBERTY_GLOBS:
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    return None


def area_plan(yosys, files: list[str], top: str, workdir: str,
              liberty: str | None, out_json: str,
              out_mapped: str | None = None) -> Plan:
    """Hierarchy-preserving liberty-mapped synth + per-module stat.

    No flatten: each module keeps its own cells, so stat.json attributes area
    per module and lists submodule instances as cell types (rollup material).
    ``out_mapped`` additionally dumps the mapped netlist JSON — cells carry
    yosys ``src`` attributes, the raw material for per-line attribution and
    dead-width pricing."""
    reads = "; ".join(f"read_verilog -sv {f}" for f in files)
    lib = f" -liberty {liberty}" if liberty else ""
    map_steps = (f"dfflibmap -liberty {liberty}; abc -liberty {liberty}; "
                 if liberty else "")
    dump = f"; write_json {out_mapped}" if out_mapped else ""
    script = (
        f"{reads}; "
        f"hierarchy -top {top}; "
        f"synth -top {top} -run coarse; "
        f"memory_map; techmap; opt -fast; "
        f"{map_steps}"
        f"tee -q -o {out_json} stat -json{lib}"
        f"{dump}"
    )
    artifacts = {"stat_json": out_json}
    if out_mapped:
        artifacts["mapped_json"] = out_mapped
    return Plan(kind="area", adapter=yosys.name, workdir=workdir,
                steps=[Step(argv=["yosys", "-p", script], cwd=workdir,
                            allow_fail=True)],
                artifacts=artifacts, meta={"top": top})


def parse_stat_json(path: str) -> dict[str, dict]:
    """{module: {area, sequential_area, cells, cells_by_type}} from yosys
    ``stat -json`` (module keys arrive with yosys's ``\\`` prefix)."""
    try:
        with open(path, "r", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for name, m in (data.get("modules") or {}).items():
        out[name.lstrip("\\")] = {
            "area_um2": round(float(m.get("area", 0.0)), 2),
            "sequential_area_um2": round(float(m.get("sequential_area", 0.0)), 2),
            "cells": int(m.get("num_cells", 0)),
            "cells_by_type": {k.lstrip("\\"): v
                              for k, v in (m.get("num_cells_by_type") or {}).items()},
        }
    return out


def demangle(name: str) -> tuple[str, str]:
    """(base_module, param_tag) for yosys-mangled parameterized modules.

    ``$paramod\\counter\\WIDTH=s32'00…01000`` → ("counter", "WIDTH=8");
    ``$paramod$<sha>\\counter`` → ("counter", "spec"); plain names pass through."""
    if not name.startswith("$paramod"):
        return name, ""
    parts = name.split("\\")
    if len(parts) >= 3:  # $paramod \ base \ P=... \ Q=...
        tags = []
        for p in parts[2:]:
            if "=" in p:
                k, v = p.split("=", 1)
                m = re.match(r"s?\d+'([01xz]+)$", v)
                if m:
                    try:
                        v = str(int(m.group(1), 2))
                    except ValueError:
                        pass
                tags.append(f"{k}={v}")
        return parts[1], ",".join(tags)
    if len(parts) == 2:  # $paramod$<sha> \ base
        return parts[1], "spec"
    return name, ""


def rollup(per_module: dict[str, dict]) -> dict[str, dict]:
    """Per-instance total area per module definition, including submodules:
    hierarchy is encoded in stat's cells_by_type (a submodule instance appears
    as a 'cell' whose type is another module definition)."""
    memo: dict[str, float] = {}

    def total(name: str, stack: tuple = ()) -> float:
        if name in memo:
            return memo[name]
        if name in stack or name not in per_module:
            return 0.0  # recursion guard / leaf lib cell (area already counted)
        m = per_module[name]
        t = m["area_um2"]
        for cell, n in m["cells_by_type"].items():
            if cell in per_module:  # submodule instance, not a lib cell
                t += n * total(cell, stack + (name,))
        memo[name] = t
        return t

    return {name: {**m, "total_area_um2": round(total(name), 2)}
            for name, m in per_module.items()}


def instance_counts(per_module: dict[str, dict], top: str) -> dict[str, int]:
    """How many times each module definition is instantiated in the design
    rooted at ``top`` (multiplicities multiply down the hierarchy)."""
    counts: dict[str, int] = {top: 1} if top in per_module else {}

    def visit(name: str, mult: int, stack: tuple) -> None:
        m = per_module.get(name)
        if m is None or name in stack:
            return
        for cell, n in m["cells_by_type"].items():
            if cell in per_module:
                counts[cell] = counts.get(cell, 0) + mult * n
                visit(cell, mult * n, stack + (name,))

    visit(top, 1, ())
    return counts


# ---- PPA snapshot / delta ------------------------------------------------------

_PPA_KEYS = ("setup_ws_ns", "hold_ws_ns", "setup_wns_ns", "hold_wns_ns",
             "cell_area_um2", "die_area_um2", "utilization", "wirelength_um",
             "power_w", "drc_violations", "lvs_errors", "antenna_violations")


def ppa_snapshot(metrics: dict, area_by_module: dict | None = None) -> dict:
    snap = {k: metrics.get(k) for k in _PPA_KEYS if metrics.get(k) is not None}
    if area_by_module:
        snap["area_by_module"] = {
            k: v.get("design_area_um2",
                     v.get("total_area_um2", v.get("area_um2")))
            for k, v in area_by_module.items()}
    return snap


def ppa_delta(current: dict, baseline: dict) -> dict:
    """Signed deltas current-vs-baseline; timing keys where + is better are
    annotated so the agent can't misread an improvement as a regression.
    Unchanged values carry no verdict."""
    delta: dict = {}
    for k in _PPA_KEYS:
        a, b = current.get(k), baseline.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d = round(a - b, 4)
            delta[k] = {"from": b, "to": a, "delta": d}
            if d == 0:
                continue
            if k.startswith(("setup_", "hold_")):
                delta[k]["better"] = d > 0  # more slack is better
            elif k in ("cell_area_um2", "die_area_um2", "wirelength_um",
                       "power_w", "drc_violations", "lvs_errors",
                       "antenna_violations"):
                delta[k]["better"] = d < 0
    cur_area = current.get("area_by_module") or {}
    base_area = baseline.get("area_by_module") or {}
    mods = {}
    for m in sorted(set(cur_area) | set(base_area)):
        a, b = cur_area.get(m), base_area.get(m)
        if a is not None and b is not None and abs(a - b) > 1e-9:
            mods[m] = {"from": b, "to": a, "delta": round(a - b, 2)}
        elif a is None or b is None:
            mods[m] = {"from": b, "to": a, "delta": None}
    if mods:
        delta["area_by_module"] = mods
    return delta
