"""Area insights: µm² per RTL line, and dead-width (coverage × area).

Per-line attribution rides on yosys ``src`` attributes in the mapped netlist:
flops keep them exactly through liberty mapping; ABC strips them from most
combinational cells, so those are attributed to the nearest downstream cell
that still knows its source (usually the endpoint register — which is what a
designer means by "the logic feeding count_q"). The unattributable remainder
is reported as such, never silently spread.

Dead-width crosses two things the platform already owns: per-bit toggle
coverage (which bits never moved across the whole workload) and the mapped
netlist + liberty areas (what each bit's flop costs). Bits that never toggle
and carry a flop are priced waste; bits that never toggle on wires/ports are
still findings (the width is oversized even if the flop lives elsewhere).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

_SRC_ONE = re.compile(r"([^\s|]+\.s?vh?):(\d+)")


@lru_cache(maxsize=8)
def liberty_cell_areas(liberty_path: str) -> dict[str, float]:
    """{cell_type: area} from a liberty file (one linear pass, cached)."""
    areas: dict[str, float] = {}
    cur = None
    cell_re = re.compile(r"^\s*cell\s*\(\s*\"?([\w]+)\"?\s*\)")
    area_re = re.compile(r"^\s*area\s*:\s*([\d.]+)")
    try:
        with open(liberty_path, "r", errors="replace") as fh:
            for ln in fh:
                m = cell_re.match(ln)
                if m:
                    cur = m.group(1)
                    continue
                if cur:
                    a = area_re.match(ln)
                    if a:
                        areas[cur] = float(a.group(1))
                        cur = None
    except OSError:
        pass
    return areas


def _first_src(attr: str) -> str:
    """'counter.sv:15.3-24.6|other.sv:9' → 'counter.sv:15' (first location,
    line only — column ranges are noise at this altitude)."""
    m = _SRC_ONE.search(attr or "")
    return f"{m.group(1).split('/')[-1]}:{m.group(2)}" if m else ""


def load_mapped_modules(mapped_json_path: str) -> dict[str, list[dict]]:
    """Cells of EVERY module in the mapped netlist (hierarchy-preserving synth
    keeps each module's cells — and src attrs — in its own namespace)."""
    try:
        with open(mapped_json_path, "r", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {name.lstrip("\\"): _module_cells(mod)
            for name, mod in (d.get("modules") or {}).items()}


def load_mapped_cells(mapped_json_path: str, top: str = "") -> list[dict]:
    """Mapped-netlist cells: {name, type, src, out_bits, in_bits, is_seq}."""
    try:
        with open(mapped_json_path, "r", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    mods = d.get("modules", {})
    mod = mods.get(top) or mods.get("\\" + top) if top else None
    if mod is None and mods:
        # largest module (paramod-mangled tops land here)
        mod = max(mods.values(), key=lambda m: len(m.get("cells", {})))
    if not mod:
        return []
    return _module_cells(mod)


def _module_cells(mod: dict) -> list[dict]:
    # liberty-mapped cells carry no port_directions in write_json — fall back
    # to the standard-cell output-pin convention (sky130/gf180/generic ABC)
    _OUT_PINS = {"X", "Y", "Q", "Q_N", "Z", "COUT", "SUM", "GCLK", "OUT"}
    out = []
    for name, c in (mod.get("cells") or {}).items():
        dirs = c.get("port_directions") or {}
        conns = c.get("connections", {})
        out_bits, in_bits = [], []
        for port, bits in conns.items():
            is_out = (dirs.get(port) == "output" if port in dirs
                      else port in _OUT_PINS)
            (out_bits if is_out else in_bits).extend(
                b for b in bits if isinstance(b, int))
        ctype = c.get("type", "").lstrip("\\")
        out.append({
            "name": name.lstrip("\\"), "type": ctype,
            "src": _first_src((c.get("attributes") or {}).get("src", "")),
            "out_bits": out_bits, "in_bits": in_bits,
            "is_seq": bool(re.search(r"df|dlx|latch|sdff|DFF|LATCH", ctype)),
        })
    # net names for dead-width pricing: bit -> RTL net name
    netnames = {}
    for net, info in (mod.get("netnames") or {}).items():
        for b in info.get("bits", []):
            if isinstance(b, int) and b not in netnames:
                netnames[b] = net.lstrip("\\")
    for c in out:
        c["out_nets"] = [netnames.get(b, "") for b in c["out_bits"]]
    return out


def area_by_line_multi(cells_by_module: dict[str, list[dict]],
                       cell_areas: dict[str, float],
                       max_hops: int = 12) -> dict:
    """Whole-design µm² per RTL line: run per-module attribution (bit
    namespaces are per module) and merge the line buckets."""
    merged: dict[str, dict] = {}
    unattr = {"area_um2": 0.0, "cells": 0}
    for cells in cells_by_module.values():
        r = area_by_line(cells, cell_areas, max_hops=max_hops)
        for line, e in r["lines"].items():
            m = merged.setdefault(line, {"area_um2": 0.0, "seq_area_um2": 0.0,
                                         "cells": 0, "attributed_cells": 0})
            for k in m:
                m[k] = round(m[k] + e[k], 4) if isinstance(m[k], float) \
                    else m[k] + e[k]
        unattr["area_um2"] = round(unattr["area_um2"]
                                   + r["unattributed"]["area_um2"], 4)
        unattr["cells"] += r["unattributed"]["cells"]
    ranked = dict(sorted(merged.items(), key=lambda kv: -kv[1]["area_um2"]))
    return {"lines": ranked, "unattributed": unattr}


def area_by_line(cells: list[dict], cell_areas: dict[str, float],
                 max_hops: int = 12) -> dict:
    """µm² per RTL source line. Cells with ``src`` book directly; the rest
    follow the netlist forward to the nearest src-carrying cell (endpoint
    flops keep src exactly). Returns lines sorted by total area, plus the
    honestly-unattributed remainder."""
    driver_of: dict[int, dict] = {}
    sinks_of: dict[int, list[dict]] = {}
    for c in cells:
        for b in c["out_bits"]:
            driver_of[b] = c
        for b in c["in_bits"]:
            sinks_of.setdefault(b, []).append(c)

    def area_of(c: dict) -> float:
        return cell_areas.get(c["type"], 0.0)

    lines: dict[str, dict] = {}

    def book(src: str, c: dict, direct: bool) -> None:
        e = lines.setdefault(src, {"area_um2": 0.0, "seq_area_um2": 0.0,
                                   "cells": 0, "attributed_cells": 0})
        e["area_um2"] = round(e["area_um2"] + area_of(c), 4)
        if c["is_seq"]:
            e["seq_area_um2"] = round(e["seq_area_um2"] + area_of(c), 4)
        e["cells"] += 1
        if not direct:
            e["attributed_cells"] += 1

    unattributed = {"area_um2": 0.0, "cells": 0}
    for c in cells:
        if c["src"]:
            book(c["src"], c, direct=True)
            continue
        # forward BFS to the nearest cell that knows its source
        seen: set[str] = {c["name"]}
        frontier = [c]
        found = ""
        for _ in range(max_hops):
            nxt: list[dict] = []
            for f in frontier:
                for b in f["out_bits"]:
                    for s in sinks_of.get(b, []):
                        if s["name"] in seen:
                            continue
                        seen.add(s["name"])
                        if s["src"]:
                            found = s["src"]
                            break
                        nxt.append(s)
                    if found:
                        break
                if found:
                    break
            if found or not nxt:
                break
            frontier = nxt
        if found:
            book(found, c, direct=False)
        else:
            unattributed["area_um2"] = round(
                unattributed["area_um2"] + area_of(c), 4)
            unattributed["cells"] += 1
    ranked = dict(sorted(lines.items(), key=lambda kv: -kv[1]["area_um2"]))
    return {"lines": ranked, "unattributed": unattributed}


# ---- dead-width (toggle coverage × flop cost) --------------------------------------

_TOGGLE_O = re.compile(r"^(?P<sig>[\w.$]+)\[(?P<bit>\d+)\]:")


def dead_bits(cov_model, cells: list[dict] | None = None,
              cell_areas: dict[str, float] | None = None) -> list[dict]:
    """Per-signal findings: bits that never toggled in the whole workload,
    priced with their flop's area where the netlist has one.

    ``cov_model`` is a CoverageModel with verilator toggle bins (per bit, per
    edge). A bit is dead only if BOTH edges count 0."""
    # (hier, signal) -> bit -> total edge count
    sigs: dict[tuple[str, str], dict[int, int]] = {}
    meta: dict[tuple[str, str], dict] = {}
    for b in cov_model.bins.values():
        if b.kind != "toggle":
            continue
        parts = b.id.split(":")
        hier = parts[-1] if len(parts) >= 5 else ""
        m = _TOGGLE_O.match(":".join(parts[3:-1]) if len(parts) >= 5 else "")
        if not m:
            continue
        key = (hier, m["sig"])
        sigs.setdefault(key, {})
        bit = int(m["bit"])
        sigs[key][bit] = sigs[key].get(bit, 0) + b.count
        meta.setdefault(key, {"source": b.source})

    # netlist pricing: RTL net name -> seq cell area per bit. ABC re-emits
    # flop nets with a "$abc$<n>$" prefix — strip it to recover the RTL name.
    ff_cost: dict[str, float] = {}
    if cells and cell_areas:
        for c in cells:
            if not c["is_seq"]:
                continue
            for net in c.get("out_nets", []):
                if net:
                    clean = re.sub(r"^\$abc\$\d+\$", "", net)
                    ff_cost[clean] = cell_areas.get(c["type"], 0.0)

    findings = []
    for (hier, sig), bits in sigs.items():
        if len(bits) < 2:
            continue  # scalar: nothing to shrink
        width = max(bits) + 1
        dead = sorted(b for b, n in bits.items() if n == 0)
        if not dead:
            continue
        wasted = 0.0
        priced = 0
        for b in dead:
            a = ff_cost.get(f"{sig}[{b}]", 0.0)
            if a:
                wasted += a
                priced += 1
        top_dead = [b for b in dead if b == width - 1 or (b + 1) in dead]
        findings.append({
            "hier": hier, "signal": sig, "width": width,
            "dead_bits": dead, "dead": len(dead),
            "contiguous_msb_dead": len(top_dead) if width - 1 in dead else 0,
            "wasted_area_um2": round(wasted, 2),
            "priced_flops": priced,
            "source": meta[(hier, sig)]["source"],
            "hint": (f"bits [{dead[0]}:{width-1}] never toggled — is the "
                     f"declared width real?" if width - 1 in dead else
                     "interior bits stuck — check stimulus or dead logic")})
    findings.sort(key=lambda f: (-f["wasted_area_um2"], -f["dead"]))
    return findings
