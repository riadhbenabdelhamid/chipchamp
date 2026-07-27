"""OpenROAD/OpenSTA detailed path-report parsing + LibreLane run-dir location.

The ``report_checks`` grammar here is shared by OpenSTA, OpenROAD's STA steps
and PrimeTime, so the same parser reads LibreLane's per-corner ``max.rpt`` /
``min.rpt`` and a standalone ``sta.run`` report.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

_START = re.compile(r"^Startpoint:\s*(?P<name>\S+)\s*(?:\((?P<kind>[^)]*)\))?")
_END = re.compile(r"^Endpoint:\s*(?P<name>\S+)\s*(?:\((?P<kind>[^)]*)\))?")
_GROUP = re.compile(r"^Path Group:\s*(\S+)")
_TYPE = re.compile(r"^Path Type:\s*(\S+)")
_SLACK = re.compile(r"^\s*(?P<slack>-?[\d.]+)\s+slack\s+\((?P<verdict>MET|VIOLATED)\)")
_CORNER = re.compile(r"=+\s*(\S+)\s+Corner\s*=+")
# "  0.615149    2.632494 v input2/X (sky130_fd_sc_hd__clkdlybuf4s25_1)"
_POINT = re.compile(
    r"(?P<delay>-?[\d.]+)\s+(?P<time>-?[\d.]+)\s+[\^v]\s+"
    r"(?P<pin>\S+/\S+)\s+\((?P<cell>[^)]+)\)\s*$")
_ARRIVAL = re.compile(r"^\s*(-?[\d.]+)\s+data arrival time")


@dataclass
class TimingPath:
    startpoint: str = ""
    startpoint_kind: str = ""  # input port | rising edge-triggered flip-flop …
    endpoint: str = ""
    endpoint_kind: str = ""
    group: str = ""
    path_type: str = ""  # max (setup) | min (hold)
    corner: str = ""
    slack: float = 0.0
    met: bool = True
    arrival: float = 0.0
    points: list[dict] = field(default_factory=list)  # {pin, cell, delay, time}

    @property
    def start_inst(self) -> str:
        return self.startpoint.split("/")[0]

    @property
    def end_inst(self) -> str:
        return self.endpoint.split("/")[0]

    def worst_stages(self, n: int = 3) -> list[dict]:
        """The n largest single-stage delays — where the picoseconds went."""
        return sorted(self.points, key=lambda p: -p["delay"])[:n]


def parse_path_report(text: str, corner: str = "") -> list[TimingPath]:
    """All paths in a ``report_checks`` report (any of OpenSTA/OpenROAD/PT)."""
    paths: list[TimingPath] = []
    cur: TimingPath | None = None
    cur_corner = corner
    for line in text.splitlines():
        cm = _CORNER.search(line)
        if cm:
            cur_corner = cm.group(1)
            continue
        sm = _START.match(line.strip())
        if sm:
            cur = TimingPath(startpoint=sm["name"],
                             startpoint_kind=(sm["kind"] or "").strip(),
                             corner=cur_corner)
            paths.append(cur)
            continue
        if cur is None:
            continue
        em = _END.match(line.strip())
        if em:
            cur.endpoint, cur.endpoint_kind = em["name"], (em["kind"] or "").strip()
            continue
        gm = _GROUP.match(line.strip())
        if gm:
            cur.group = gm.group(1)
            continue
        tm = _TYPE.match(line.strip())
        if tm:
            cur.path_type = tm.group(1)
            continue
        pm = _POINT.search(line)
        if pm:
            cur.points.append({"pin": pm["pin"], "cell": pm["cell"],
                               "delay": float(pm["delay"]),
                               "time": float(pm["time"])})
            continue
        am = _ARRIVAL.match(line)
        if am:
            if not cur.arrival:  # the summary recap repeats it negated
                cur.arrival = float(am.group(1))
            continue
        km = _SLACK.match(line)
        if km:
            cur.slack = float(km["slack"])
            cur.met = km["verdict"] == "MET"
    return paths


# ---- LibreLane run-dir navigation ---------------------------------------------


def find_run_dir(pd_root: str, module: str, run: str = "") -> str | None:
    """Newest (or named) LibreLane run dir for a module under
    ``<dot>/pd/<module>/runs/``."""
    base = os.path.join(pd_root, module, "runs")
    if run:
        cand = os.path.join(base, run)
        return cand if os.path.isdir(cand) else None
    runs = [d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d)]
    return max(runs, key=os.path.getmtime) if runs else None


def _newest_step(run_dir: str, suffix: str) -> str | None:
    steps = sorted(glob.glob(os.path.join(run_dir, f"*-{suffix}")))
    return steps[-1] if steps else None


def find_sta_reports(run_dir: str, corner_glob: str = "nom_*") -> dict[str, dict]:
    """Per-corner {corner: {max: path, min: path}} from the final post-PnR STA
    step (falls back to any ``openroad-sta*`` step)."""
    step = (_newest_step(run_dir, "openroad-stapostpnr")
            or _newest_step(run_dir, "openroad-stamidpnr-3")
            or _newest_step(run_dir, "openroad-stamidpnr-1"))
    if not step:
        return {}
    out: dict[str, dict] = {}
    for cdir in sorted(glob.glob(os.path.join(step, corner_glob))):
        if not os.path.isdir(cdir):
            continue
        corner = os.path.basename(cdir)
        entry = {}
        for kind in ("max", "min"):
            rpt = os.path.join(cdir, f"{kind}.rpt")
            if os.path.exists(rpt):
                entry[kind] = rpt
        if entry:
            out[corner] = entry
    return out


def find_synth_netlist(run_dir: str, module: str) -> str | None:
    """The yosys-synthesis netlist — instance names are stable through PnR and
    its nets still carry RTL names (the final netlist's are buffer-renamed)."""
    step = _newest_step(run_dir, "yosys-synthesis")
    if not step:
        return None
    cand = os.path.join(step, f"{module}.nl.v")
    if os.path.exists(cand):
        return cand
    hits = glob.glob(os.path.join(step, "*.nl.v"))
    return hits[0] if hits else None
