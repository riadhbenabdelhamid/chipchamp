"""Synthesis-strategy sweep → Pareto frontier (area vs delay vs power).

Two rungs, matching how designers actually work:

- **synth mode** (seconds): one yosys run per strategy — the ABC mapping
  script is the knob that moves QoR (measured on this machine: ``map -a``
  vs ``map`` differ >50% in area on a MAC) — then one OpenSTA pass per
  mapped netlist for a REAL worst-path delay in ns (no liberty-blind depth
  proxies). Fast enough to run on every RTL iteration.
- **pnr mode** (minutes/config): full LibreLane runs per ``SYNTH_STRATEGY``,
  compared on post-route numbers via the run metrics.

The Pareto marking is plain dominance: a config survives if nothing else is
at least as good on every axis and better on one.
"""
from __future__ import annotations

import os

from ..adapters.base import Plan, Step

# name -> yosys abc invocation tail (the mapping-script knob)
SYNTH_CONFIGS = {
    "default": "",                              # yosys's &nf flow (area-lean)
    "area":    "-script +strash;dch;map,-a",    # area-oriented mapping
    "delay":   "-script +strash;dch;map",       # delay-oriented mapping
}

# LibreLane SYNTH_STRATEGY values worth sweeping (pnr mode)
PNR_STRATEGIES = ["AREA 0", "AREA 2", "DELAY 0", "DELAY 2"]


def synth_config_plan(yosys, files: list[str], top: str, workdir: str,
                      liberty: str, name: str, abc_tail: str,
                      out_stat: str, out_netlist: str) -> Plan:
    reads = "; ".join(f"read_verilog -sv {f}" for f in files)
    abc = f"abc -liberty {liberty} {abc_tail}".strip()
    script = (f"{reads}; hierarchy -top {top}; synth -top {top} -run coarse; "
              f"memory_map; techmap; opt -fast; "
              f"dfflibmap -liberty {liberty}; {abc}; "
              f"tee -q -o {out_stat} stat -json -liberty {liberty}; "
              f"write_verilog -noattr {out_netlist}")
    return Plan(kind="area", adapter=yosys.name, workdir=workdir,
                steps=[Step(argv=["yosys", "-p", script], cwd=workdir,
                            allow_fail=True)],
                artifacts={"stat_json": out_stat, "netlist": out_netlist},
                meta={"top": top, "config": name})


DELAY_TCL = """\
read_liberty {liberty}
read_verilog {netlist}
link_design {top}
create_clock -name {clock} -period {period} [get_ports {clock}]
report_checks -path_delay max -digits 4
"""


def delay_tcl(liberty: str, netlist: str, top: str, clock: str,
              period_ns: float) -> str:
    return DELAY_TCL.format(liberty=liberty, netlist=netlist, top=top,
                            clock=clock, period=period_ns)


def pareto(points: list[dict], axes: dict[str, str]) -> list[dict]:
    """Mark each point ``pareto: True/False`` under the axis objectives
    ({key: 'min'|'max'}). Points missing an axis value never dominate on it
    and can't be dominated on it (None-safe)."""
    def better_eq(a, b, key, goal):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            return va is not None or vb is None  # missing loses ties
        return va <= vb if goal == "min" else va >= vb

    def strictly_better(a, b, key, goal):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            return va is not None and vb is None
        return va < vb if goal == "min" else va > vb

    for p in points:
        dominated = False
        for q in points:
            if q is p:
                continue
            if all(better_eq(q, p, k, g) for k, g in axes.items()) and \
                    any(strictly_better(q, p, k, g) for k, g in axes.items()):
                dominated = True
                break
        p["pareto"] = not dominated
    return points
