"""FPGA design-checkpoint intelligence: nextpnr + Vivado report parsing,
checkpoint snapshots, and the same baseline/delta feedback the ASIC side has.

A *checkpoint* is a queryable snapshot of the design at a flow stage —
Vivado's ``.dcp`` (post_synth / post_place / post_route) or the open flow's
stage artifacts (yosys JSON post-synth, nextpnr JSON + report post-route).
Every checkpoint carries normalized metrics (fmax/WNS, utilization by
resource, power) so ``fpga.ppa`` can answer "what did that RTL edit cost" and
``fpga.critical`` can point the worst path at RTL ``file:line``.
"""
from __future__ import annotations

import json
import os
import re

# ---- nextpnr -------------------------------------------------------------------


def parse_nextpnr_report(path: str) -> dict:
    """Normalize nextpnr's ``--report`` JSON: fmax per clock (achieved vs
    constraint), utilization {bel: {used, available}}, and critical paths with
    per-segment delays + native RTL source refs (yosys src attributes)."""
    try:
        with open(path, "r", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict = {"fmax": {}, "utilization": {}, "critical_paths": []}
    for clk, v in (d.get("fmax") or {}).items():
        out["fmax"][clk] = {"achieved_mhz": round(float(v.get("achieved", 0)), 2),
                            "constraint_mhz": round(float(v.get("constraint", 0)), 2)}
    for bel, v in (d.get("utilization") or {}).items():
        used = int(v.get("used", 0))
        if used:
            out["utilization"][bel] = {"used": used,
                                       "available": int(v.get("available", 0))}
    for p in d.get("critical_paths") or []:
        segs = []
        total = 0.0
        for s in p.get("path") or []:
            delay = float(s.get("delay", 0.0))
            total += delay
            segs.append({
                "type": s.get("type", ""),
                "delay_ns": round(delay, 3),
                "cell": (s.get("from") or {}).get("cell", ""),
                "net": s.get("net", ""),
                "sources": s.get("sources") or [],
            })
        out["critical_paths"].append({
            "clock": p.get("to", p.get("from", "")),
            "delay_ns": round(total, 3),
            "segments": segs,
        })
    return out


_LOG_FMAX = re.compile(
    r"Max frequency for clock\s+'(?P<clk>[^']+)':\s*(?P<mhz>[\d.]+)\s*MHz"
    r"\s*\((?P<verdict>PASS|FAIL) at (?P<target>[\d.]+)\s*MHz\)")


def parse_nextpnr_log_fmax(text: str) -> dict:
    """fmax per clock from a nextpnr LOG (for flows that keep the log but not
    the report — e.g. FABulous). The last occurrence per clock is the final
    (post-route) timing."""
    out: dict = {}
    for m in _LOG_FMAX.finditer(text):
        out[m["clk"]] = {"achieved_mhz": round(float(m["mhz"]), 2),
                         "constraint_mhz": round(float(m["target"]), 2),
                         "met": m["verdict"] == "PASS"}
    return out


def worst_fmax(report: dict) -> tuple[str, float, float] | None:
    """(clock, achieved_mhz, constraint_mhz) for the tightest clock."""
    best = None
    for clk, v in (report.get("fmax") or {}).items():
        margin = v["achieved_mhz"] - v["constraint_mhz"]
        if best is None or margin < best[1] - best[2]:
            best = (clk, v["achieved_mhz"], v["constraint_mhz"])
    return best


def rtl_sources_of_path(path: dict, top_delays: int = 3) -> list[dict]:
    """The heaviest segments of a nextpnr critical path that carry RTL source
    refs — the cross-probe, straight from the report."""
    segs = [s for s in path.get("segments", []) if s.get("sources")]
    segs.sort(key=lambda s: -s["delay_ns"])
    out = []
    for s in segs[:top_delays]:
        out.append({"delay_ns": s["delay_ns"], "cell": s["cell"],
                    "net": s["net"], "rtl": s["sources"]})
    return out


# ---- Vivado report grammar -------------------------------------------------------

_VIV_SLACK = re.compile(
    r"^Slack\s*\((?P<verdict>MET|VIOLATED)\)\s*:\s*(?P<slack>-?[\d.]+)ns")
_VIV_SRC = re.compile(r"^\s*Source:\s*(?P<p>\S+)")
_VIV_DST = re.compile(r"^\s*Destination:\s*(?P<p>\S+)")
_VIV_DPD = re.compile(
    r"^\s*Data Path Delay:\s*(?P<d>[\d.]+)ns\s*\(logic\s*(?P<logic>[\d.]+)ns"
    r"\s*\([\d.]+%\)\s*route\s*(?P<route>[\d.]+)ns")
_VIV_LVL = re.compile(r"^\s*Logic Levels:\s*(?P<n>\d+)\s*(?:\((?P<mix>[^)]*)\))?")
_VIV_UTIL_ROW = re.compile(
    r"^\|\s*(?P<name>[A-Za-z][\w ./+-]*?)\s*\|\s*(?P<used>\d+)\s*\|"
    r"\s*\d+\s*\|\s*\d+\s*\|\s*(?P<avail>\d+)\s*\|")
_VIV_POWER = re.compile(
    r"^\|\s*Total On-Chip Power \(W\)\s*\|\s*(?P<w>[\d.]+)")


def parse_vivado_timing_summary(text: str) -> dict:
    """WNS/TNS/WHS/THS (+ pulse width) from ``report_timing_summary`` — the
    numeric row after the 'Design Timing Summary' banner."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if "Design Timing Summary" not in ln:
            continue
        for j in range(i + 1, min(i + 10, len(lines))):
            nums = re.findall(r"-?\d+\.\d+|-?\d+\b", lines[j])
            if len(nums) >= 8 and "." in lines[j]:
                vals = [float(x) for x in nums[:8]]
                return {"wns_ns": vals[0], "tns_ns": vals[1],
                        "failing_endpoints": int(vals[2]),
                        "total_endpoints": int(vals[3]),
                        "whs_ns": vals[4], "ths_ns": vals[5]}
    return {}


# canonical resources reported by fpga.ppa (Vivado row name → key)
_VIV_RESOURCES = {
    "Slice LUTs": "lut", "CLB LUTs": "lut",
    "Slice Registers": "ff", "CLB Registers": "ff",
    "Block RAM Tile": "bram", "DSPs": "dsp", "DSP Slices": "dsp",
    "Bonded IOB": "io", "CARRY4": "carry", "CARRY8": "carry",
    "F7 Muxes": "muxf",
}


def parse_vivado_utilization(text: str) -> dict:
    out: dict = {}
    for ln in text.splitlines():
        m = _VIV_UTIL_ROW.match(ln)
        if not m:
            continue
        key = _VIV_RESOURCES.get(m["name"].strip())
        if key and key not in out:  # first table (summary) wins
            out[key] = {"used": int(m["used"]), "available": int(m["avail"])}
    return out


def parse_vivado_power(text: str) -> dict:
    for ln in text.splitlines():
        m = _VIV_POWER.match(ln)
        if m:
            return {"power_w": float(m["w"])}
    return {}


def parse_vivado_paths(text: str) -> list[dict]:
    """report_timing path blocks → [{slack_ns, met, source, destination,
    datapath_ns, logic_ns, route_ns, logic_levels, level_mix}] with the RTL
    register behind each endpoint (``count_q_reg[3]/C`` → ``count_q``)."""
    paths: list[dict] = []
    cur: dict | None = None
    for ln in text.splitlines():
        sm = _VIV_SLACK.match(ln.strip())
        if sm:
            cur = {"slack_ns": float(sm["slack"]),
                   "met": sm["verdict"] == "MET"}
            paths.append(cur)
            continue
        if cur is None:
            continue
        for rex, keys in ((_VIV_SRC, ("source",)), (_VIV_DST, ("destination",))):
            m = rex.match(ln)
            if m:
                cur[keys[0]] = m["p"]
                cur[f"{keys[0]}_rtl"] = vivado_rtl_name(m["p"])
        m = _VIV_DPD.match(ln)
        if m:
            cur.update(datapath_ns=float(m["d"]), logic_ns=float(m["logic"]),
                       route_ns=float(m["route"]))
        m = _VIV_LVL.match(ln)
        if m:
            cur["logic_levels"] = int(m["n"])
            if m["mix"]:
                cur["level_mix"] = m["mix"]
    return paths


def vivado_rtl_name(endpoint: str) -> str:
    """``u_core/count_q_reg[3]/C`` → ``u_core.count_q`` — strip the pin, the
    ``_reg`` suffix Vivado appends to inferred registers, and bit selects."""
    p = endpoint.split("/")
    if len(p) > 1 and re.fullmatch(r"[A-Z][A-Z0-9_]*", p[-1]):
        p = p[:-1]  # drop pin (C/D/Q/CE...)
    name = ".".join(p)
    name = re.sub(r"\[\d+\]$", "", name)
    name = re.sub(r"_reg(?:\[\d+\])?$", "", name)
    return name


# ---- checkpoint snapshot + delta -------------------------------------------------

# for delta direction: True = higher is better
_FPGA_BETTER_UP = {"fmax_mhz": True, "wns_ns": True, "whs_ns": True}


def checkpoint_snapshot(stage: str, *, fmax: dict | None = None,
                        timing: dict | None = None,
                        utilization: dict | None = None,
                        power: dict | None = None) -> dict:
    """One normalized checkpoint record. Open flow supplies ``fmax``
    ({clock:{achieved,constraint}}); Vivado supplies ``timing`` (WNS/TNS)."""
    snap: dict = {"stage": stage}
    if fmax:
        worst = min(fmax.values(),
                    key=lambda v: v["achieved_mhz"] - v["constraint_mhz"])
        snap["fmax_mhz"] = worst["achieved_mhz"]
        snap["fmax_constraint_mhz"] = worst["constraint_mhz"]
        snap["clocks"] = fmax
    if timing:
        snap.update({k: v for k, v in timing.items() if k in
                     ("wns_ns", "tns_ns", "whs_ns", "ths_ns",
                      "failing_endpoints")})
    for res, v in (utilization or {}).items():
        snap[f"{res}_used"] = v["used"]
        snap[f"{res}_avail"] = v["available"]
    if power:
        snap.update(power)
    return snap


def checkpoint_delta(current: dict, baseline: dict) -> dict:
    """Signed deltas between two checkpoints of the same stage, annotated
    better/worse (fmax/slack up = better; used resources/power up = worse)."""
    delta: dict = {}
    keys = (set(current) | set(baseline)) - {"stage", "clocks"}
    for k in sorted(keys):
        a, b = current.get(k), baseline.get(k)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        d = round(a - b, 4)
        entry: dict = {"from": b, "to": a, "delta": d}
        if d != 0:
            if k in _FPGA_BETTER_UP:
                entry["better"] = d > 0
            elif k.endswith("_used") or k in ("power_w", "tns_ns_mag",
                                              "failing_endpoints"):
                entry["better"] = d < 0
            elif k == "tns_ns":
                entry["better"] = d > 0  # TNS is ≤0; toward 0 is better
        delta[k] = entry
    return delta
