"""Verification ladder + gates (SPEC §10.1, §11.1).

The ladder (V0 parse → V4 signoff) is policy-bound to task classes: each class
declares the minimum rungs and mandatory extra gates that must pass before the
agent may report success. Gate evaluation reads *job records* (and the coverage
baseline / anti-gaming findings), never model narration — this is the mechanism
behind FR-CORE-02: the agent is structurally unable to claim done without them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ladder rung -> human label
LADDER = {
    "V0": "parse + elaborate",
    "V1": "lint + style + CDC-lite",
    "V2": "smoke simulation",
    "V3": "affected regression + coverage",
    "V4": "signoff battery",
    "P":  "physical signoff (DRC/LVS/timing/antenna via RTL-to-GDSII)",
    "E":  "eFPGA-FABulous (fabric generation + user-design bitstream)",
    "R":  "RISC-V core (functional rigor + ISA conformance vs a reference model)",
    "F":  "FPGA implementation (synth/place/route → fits, timing, bitstream)",
}

# task class -> (min ladder rung, required gate names)
CLASS_GATES: dict[str, tuple[str, list[str]]] = {
    "docs": ("V0", []),
    "tb_only": ("V2", ["elab", "smoke_sim", "no_gaming"]),
    "build_scripts": ("V2", ["elab", "smoke_sim", "no_gaming"]),
    "regmap": ("V3", ["lint", "smoke_sim", "affected_regress",
                      "coverage_baseline", "regmap_regen", "no_gaming"]),
    "rtl_nfc": ("V2", ["lint", "smoke_sim", "lec", "no_gaming"]),
    "rtl_functional": ("V3", ["lint", "smoke_sim", "affected_regress",
                              "coverage_baseline", "no_gaming"]),
    "timing": ("V3", ["lint", "smoke_sim", "affected_regress",
                      "coverage_baseline", "sta_delta", "no_gaming"]),
    "cdc": ("V3", ["lint", "smoke_sim", "affected_regress",
                   "coverage_baseline", "cdc_clean", "no_gaming"]),
    "physical": ("P", ["drc_clean", "lvs_clean", "timing_met", "antenna_clean"]),
    "efpga-fabulous": ("E", ["fabric_generated", "bitstream_generated"]),
    "riscv": ("R", ["lint", "smoke_sim", "affected_regress",
                    "coverage_baseline", "isa_cosim", "isa_compliant",
                    "no_gaming"]),
    "fpga": ("F", ["fpga_routed", "fpga_fits", "fpga_timing_met",
                   "fpga_bitstream"]),
}


@dataclass
class Evidence:
    """What gate evaluation reads. Everything here is machine-produced."""
    jobs: list = field(default_factory=list)  # JobRecord
    coverage_baseline: Optional[object] = None  # BaselineVerdict
    antigaming: list[dict] = field(default_factory=list)  # {kind,detail,acknowledged}
    regmap_regenerated: Optional[bool] = None
    cdc_new_violations: Optional[int] = None
    sta_new_violations: Optional[int] = None
    # fpga_fits budget: 1.0 = "must fit" (never a false failure); tighten via
    # config [fpga] max_utilization to demand headroom.
    fpga_max_utilization: float = 1.0
    # RISC-V: set by riscv.cosim / riscv.compliance via the tool context
    riscv_cosim: Optional[dict] = None
    riscv_compliance: Optional[dict] = None

    def jobs_of(self, kind: str) -> list:
        return [j for j in self.jobs if j.kind == kind]


@dataclass
class GateResult:
    name: str
    status: str  # pass | fail | missing
    evidence: str = ""  # job id or check reference
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class GateReport:
    task_class: str
    min_rung: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(g.ok for g in self.gates)

    @property
    def blocking(self) -> list[str]:
        return [g.name for g in self.gates if not g.ok]

    def to_dict(self) -> dict:
        return {"task_class": self.task_class, "min_rung": self.min_rung,
                "all_passed": self.all_passed, "blocking": self.blocking,
                "gates": [g.__dict__ for g in self.gates]}


def required_gates(task_class: str) -> tuple[str, list[str]]:
    return CLASS_GATES.get(task_class, ("V1", ["elab", "no_gaming"]))


def _passed_job(evidence: Evidence, kinds: list[str], predicate=None) -> Optional[object]:
    for j in evidence.jobs:
        if j.kind in kinds and j.status == "passed":
            if predicate is None or predicate(j):
                return j
    return None


def _sim_identity(j) -> str:
    import os
    for f in reversed(getattr(j, "input_files", []) or []):
        if "tb" in os.path.basename(f).lower() or "test" in f.lower():
            return os.path.basename(f)
    waves = j.artifacts.get("waves", "") if hasattr(j, "artifacts") else ""
    return os.path.basename(waves) or (j.input_files[-1] if getattr(j, "input_files", None) else j.id)


def _efpga_metrics(evidence: Evidence) -> Optional[dict]:
    """Metrics from the latest eFPGA-FABulous job (fabric/bitstream)."""
    jobs = evidence.jobs_of("efpga-fabulous")
    if not jobs:
        return None
    latest = max(jobs, key=lambda j: getattr(j, "start_ts", 0))
    return latest.result.get("metrics")


def _pnr_norm(evidence: Evidence) -> Optional[dict]:
    """Normalized physical-signoff metrics from the latest RTL-to-GDSII job."""
    pnr = evidence.jobs_of("pnr")
    if not pnr:
        return None
    latest = max(pnr, key=lambda j: getattr(j, "start_ts", 0))
    return latest.result.get("metrics")


def _latest_sims(evidence: Evidence) -> list:
    """Latest sim per testbench identity — so a test that failed and was then
    fixed+rerun counts by its most recent result (the fix-then-rerun flow).

    "Latest" is SUBMISSION ORDER, not wall-clock. A job served from the cache
    is the original record and carries the original `start_ts`, so ordering by
    timestamp ranked a just-delivered pass *below* the failure that preceded
    it — and the fix-then-rerun flow this exists for rejected a fix that
    genuinely worked. That is exactly what happens when a fix restores a file
    to a state already simulated once. `evidence.jobs` is appended in the order
    the task submitted them, which is the order that decides.
    """
    by: dict[str, object] = {}
    for j in evidence.jobs_of("sim"):
        by[_sim_identity(j)] = j        # later submission wins, unconditionally
    return list(by.values())


def evaluate(task_class: str, evidence: Evidence) -> GateReport:
    rung, gates = required_gates(task_class)
    report = GateReport(task_class=task_class, min_rung=rung)
    for name in gates:
        report.gates.append(_eval_one(name, evidence))
    return report


def _eval_one(name: str, ev: Evidence) -> GateResult:
    if name == "elab":
        j = _passed_job(ev, ["elab", "lint", "sim", "coverage"])
        return _mk(name, j, "no successful parse/elaborate job on record")
    if name == "lint":
        # a lint job with no UNWAIVED errors (waivers are data — §12.6; only
        # active, approved waivers suppress)
        for j in ev.jobs_of("lint"):
            errs = [d for d in j.result.get("diagnostics", [])
                    if d.get("severity") == "error" and not d.get("waiver")]
            if not errs:
                waived = sum(1 for d in j.result.get("diagnostics", [])
                             if d.get("waiver"))
                detail = "0 lint errors" + (f" ({waived} waived)" if waived else "")
                return GateResult(name, "pass", j.id, detail)
        return GateResult(name, "missing", "", "no clean lint job on record")
    if name == "smoke_sim":
        latest = _latest_sims(ev)
        passed = [j for j in latest if j.status == "passed"]
        if passed:
            return GateResult(name, "pass", passed[0].id,
                              f"{len(passed)} testbench(es) passing (latest run)")
        return GateResult(name, "missing" if not latest else "fail", "",
                          "no passing smoke simulation on record")
    if name == "affected_regress":
        # judge by the LATEST result per testbench (fix-then-rerun aware)
        latest = _latest_sims(ev)
        if latest and all(j.status == "passed" for j in latest):
            return GateResult(name, "pass", ",".join(j.id for j in latest[:3]),
                              f"{len(latest)} affected test(s) passing")
        failed = [j.id for j in latest if j.status != "passed"]
        return GateResult(name, "fail" if latest else "missing", "",
                          f"failing tests: {failed}" if failed else "no regression run")
    if name == "coverage_baseline":
        bv = ev.coverage_baseline
        if bv is None:
            return GateResult(name, "missing", "", "no coverage baseline comparison")
        return GateResult(name, "pass" if bv.ok else "fail", "coverage",
                          bv.summary)
    if name == "lec":
        j = _passed_job(ev, ["lec"], lambda x: x.result.get("status") == "equivalent")
        if j:
            return GateResult(name, "pass", j.id, "LEC: equivalent")
        # an inconclusive LEC must NOT pass (SPEC P7: downgrade to functional)
        inconcl = [j for j in ev.jobs_of("lec")
                   if j.result.get("status") == "inconclusive"]
        if inconcl:
            return GateResult(name, "fail", inconcl[0].id,
                              "LEC inconclusive — reclassify as functional change")
        return GateResult(name, "missing", "", "no LEC on record")
    if name == "cdc_clean":
        if ev.cdc_new_violations is None:
            return GateResult(name, "missing", "", "no CDC analysis on record")
        return GateResult(name, "pass" if ev.cdc_new_violations == 0 else "fail",
                          "cdc", f"{ev.cdc_new_violations} new crossing(s)")
    if name == "sta_delta":
        if ev.sta_new_violations is None:
            return GateResult(name, "missing", "", "no STA delta on record")
        return GateResult(name, "pass" if ev.sta_new_violations == 0 else "fail",
                          "sta", f"{ev.sta_new_violations} new timing violation(s)")
    if name in ("drc_clean", "lvs_clean", "timing_met", "antenna_clean"):
        norm = _pnr_norm(ev)
        if norm is None:
            return GateResult(name, "missing", "", "no RTL-to-GDSII (pnr) run on record")
        job = next((j for j in ev.jobs_of("pnr") if j.status in ("passed", "failed")), None)
        jid = job.id if job else "pnr"
        if name == "drc_clean":
            n = norm.get("drc_violations")
            return GateResult(name, "pass" if not n else "fail", jid,
                              f"{n if n is not None else '?'} DRC violation(s)")
        if name == "lvs_clean":
            n = norm.get("lvs_errors")
            return GateResult(name, "pass" if not n else "fail", jid,
                              f"{n if n is not None else '?'} LVS error(s)")
        if name == "antenna_clean":
            n = norm.get("antenna_violations")
            return GateResult(name, "pass" if not n else "fail", jid,
                              f"{n if n is not None else '?'} antenna net(s)")
        if name == "timing_met":
            s, h = norm.get("setup_ws_ns"), norm.get("hold_ws_ns")
            ok = (s is None or s >= 0) and (h is None or h >= 0)
            return GateResult(name, "pass" if ok else "fail", jid,
                              f"setup WNS {s} ns, hold WHS {h} ns")
    if name == "isa_cosim":
        c = ev.riscv_cosim
        if not c:
            return GateResult(name, "missing", "",
                              "no ISA co-simulation on record — run riscv.cosim")
        depth = int(c.get("compared") or 0)
        if not c.get("match"):
            d = c.get("divergence") or {}
            return GateResult(name, "fail", c.get("job", ""),
                              c.get("summary") or f"diverged at #{d.get('index')}")
        if depth <= 0:
            return GateResult(name, "fail", c.get("job", ""),
                              "no instructions were actually compared")
        detail = f"{depth} retired instructions matched the reference"
        if c.get("partial"):
            # honest: a prefix match is evidence only as far as it goes
            detail += " (PREFIX — one stream ended first)"
        return GateResult(name, "pass", c.get("job", ""), detail)
    if name == "isa_compliant":
        r = ev.riscv_compliance
        if not r:
            return GateResult(name, "missing", "",
                              "no compliance run on record — run riscv.compliance")
        total = int(r.get("total") or 0)
        failed = int(r.get("failed") or 0)
        if total <= 0:
            return GateResult(name, "fail", r.get("job", ""),
                              "the compliance suite ran no tests")
        if failed:
            return GateResult(name, "fail", r.get("job", ""),
                              f"{failed}/{total} architectural tests failed: "
                              + ", ".join((r.get("failures") or [])[:5]))
        return GateResult(name, "pass", r.get("job", ""),
                          f"{total} architectural tests passed"
                          + (f" ({r['suite']})" if r.get("suite") else ""))
    if name == "fpga_bitstream":
        jobs = ev.jobs_of("fpga_bitstream")
        if not jobs:
            return GateResult(name, "missing", "",
                              "no bitstream packed — run fpga.bitstream")
        latest = max(jobs, key=lambda j: getattr(j, "start_ts", 0))
        m = latest.result.get("metrics", {})
        size = m.get("bitstream_bytes") or 0
        ok = latest.status == "passed" and size > 0
        return GateResult(name, "pass" if ok else "fail", latest.id,
                          f"{size} bytes via {m.get('packer', '?')}" if size
                          else "no bitstream produced")
    if name in ("fpga_routed", "fpga_timing_met", "fpga_fits"):
        jobs = ev.jobs_of("fpga")
        if not jobs:
            return GateResult(name, "missing", "", "no FPGA implementation job on record")
        latest = max(jobs, key=lambda j: getattr(j, "start_ts", 0))
        if name == "fpga_fits":
            util = _fpga_utilization(latest.result.get("metrics", {}))
            if not util:
                return GateResult(name, "missing", latest.id,
                                  "no utilization data in the job record")
            budget = ev.fpga_max_utilization or 1.0
            ratios = {r: used / avail for r, (used, avail) in util.items()}
            worst = max(ratios, key=lambda r: ratios[r])
            used, avail = util[worst]
            detail = f"tightest {worst} {used}/{avail} ({ratios[worst]:.0%})"
            over = [r for r, x in ratios.items() if x > budget]
            if over:
                return GateResult(name, "fail", latest.id,
                                  f"{detail} — over budget ({budget:.0%}): "
                                  + ", ".join(sorted(over)))
            if budget < 1.0:
                detail += f", budget {budget:.0%}"
            return GateResult(name, "pass", latest.id, detail)
        m = latest.result.get("metrics", {})
        stages = m.get("stages", [])
        if name == "fpga_routed":
            ok = "post_route" in stages
            return GateResult(name, "pass" if ok else "fail", latest.id,
                              f"checkpoints: {', '.join(stages) or 'none'}")
        # fpga_timing_met: vendor flow reports WNS; open flow fmax vs constraint
        if "timing_met" in m:
            ok = bool(m["timing_met"])
            detail = (f"WNS {m.get('post_route.wns_ns')} ns, "
                      f"WHS {m.get('post_route.whs_ns')} ns")
        elif "fmax_met" in m:
            ok = bool(m["fmax_met"])
            detail = (f"fmax {m.get('fmax_mhz')} MHz vs "
                      f"{m.get('fmax_constraint_mhz')} MHz target")
        else:
            return GateResult(name, "fail", latest.id, "no timing verdict in job")
        return GateResult(name, "pass" if ok else "fail", latest.id, detail)
    if name in ("fabric_generated", "bitstream_generated"):
        m = _efpga_metrics(ev)
        if m is None:
            return GateResult(name, "missing", "", "no eFPGA-FABulous run on record")
        job = next((j for j in ev.jobs_of("efpga-fabulous")), None)
        jid = job.id if job else "efpga-fabulous"
        if name == "fabric_generated":
            ok = bool(m.get("fabric_generated")) or m.get("bitstream_bytes", 0) > 0
            return GateResult(name, "pass" if ok else "fail", jid,
                              f"{m.get('fabric_files', '?')} fabric HDL file(s)")
        if name == "bitstream_generated":
            n = m.get("bitstream_bytes", 0)
            routed = m.get("routed")
            ok = n > 0 and routed is not False
            return GateResult(name, "pass" if ok else "fail", jid,
                              f"{n} byte bitstream, routed={routed}")
    if name == "regmap_regen":
        if ev.regmap_regenerated is None:
            return GateResult(name, "missing", "", "regmap regeneration not verified")
        return GateResult(name, "pass" if ev.regmap_regenerated else "fail",
                          "regmap", "generated outputs match source of truth")
    if name == "no_gaming":
        unack = [f for f in ev.antigaming if not f.get("acknowledged")]
        if unack:
            return GateResult(name, "fail", "",
                              f"{len(unack)} unacknowledged anti-gaming finding(s): "
                              + ", ".join(f["kind"] for f in unack[:4]))
        return GateResult(name, "pass", "", "no unacknowledged gaming findings")
    return GateResult(name, "missing", "", "unknown gate")


def _fpga_utilization(m: dict) -> dict:
    """{resource: (used, available)} from an fpga job's metrics. The open flow
    reports bare ``<bel>_used``/``_avail``; Vivado prefixes the stage, and only
    the routed stage counts (earlier stages are estimates)."""
    out: dict = {}
    for k, v in m.items():
        if not k.endswith("_used") or not isinstance(v, (int, float)):
            continue
        base = k[:-len("_used")]
        if base.startswith(("post_synth.", "post_place.")):
            continue
        avail = m.get(base + "_avail")
        if not isinstance(avail, (int, float)) or avail <= 0:
            continue
        out[base.split(".", 1)[-1]] = (int(v), int(avail))
    return out


def _mk(name: str, job, missing_msg: str) -> GateResult:
    if job is None:
        return GateResult(name, "missing", "", missing_msg)
    return GateResult(name, "pass", job.id, job.summary)
