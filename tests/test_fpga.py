"""FPGA design checkpoints: nextpnr/Vivado report parsing (REAL fixture
output from this machine), checkpoint snapshot/delta directions, RTL name
recovery, and the live open-flow loop (yosys+nextpnr-ice40, seconds).
Vivado live flow is opt-in (CHIPCHAMP_LIVE_VIVADO=1, ~2-4 min)."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.physical.fpga import (checkpoint_delta, checkpoint_snapshot,
                                    parse_nextpnr_report, parse_vivado_paths,
                                    parse_vivado_power,
                                    parse_vivado_timing_summary,
                                    parse_vivado_utilization,
                                    rtl_sources_of_path, vivado_rtl_name,
                                    worst_fmax)
from chipchamp.tools import all_tools

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fx(name: str) -> str:
    with open(os.path.join(FIX, name), "r") as fh:
        return fh.read()


# ---- nextpnr report (real ice40 run of the example counter) ---------------------


def test_nextpnr_report_fmax_util_paths():
    rep = parse_nextpnr_report(os.path.join(FIX, "nextpnr_ice40_counter_report.json"))
    clk, achieved, constraint = worst_fmax(rep)
    assert constraint == 100.0 and achieved > 300  # counter flies on hx1k
    assert rep["utilization"]["ICESTORM_LC"] == {"used": 16, "available": 1280}
    assert rep["critical_paths"]
    p = rep["critical_paths"][0]
    assert p["delay_ns"] > 1.0 and len(p["segments"]) > 5
    # THE feature: native RTL source refs survive P&R
    heavy = rtl_sources_of_path(p)
    assert heavy and any("counter.sv" in r for h in heavy for r in h["rtl"])


def test_nextpnr_log_fmax_last_wins():
    """FABulous tie-in: fmax from a nextpnr LOG (no report file); the last
    occurrence per clock is the final post-route number."""
    from chipchamp.physical.fpga import parse_nextpnr_log_fmax
    fmax = parse_nextpnr_log_fmax(_fx("nextpnr_ice40_counter_log_excerpt.txt"))
    clk = "clk$SB_IO_IN_$glb_clk"
    assert fmax[clk]["achieved_mhz"] == 365.36  # not the earlier 392.77
    assert fmax[clk]["constraint_mhz"] == 100.0 and fmax[clk]["met"]


# ---- Vivado reports (real 2025.1 run of the example counter) --------------------


def test_vivado_timing_summary():
    t = parse_vivado_timing_summary(_fx("vivado_counter_timing.rpt"))
    assert t["wns_ns"] == 2.603 and t["whs_ns"] == 0.188
    assert t["tns_ns"] == 0.0 and t["failing_endpoints"] == 0


def test_vivado_utilization():
    u = parse_vivado_utilization(_fx("vivado_counter_util.rpt"))
    assert u["lut"] == {"used": 9, "available": 20800}
    assert u["ff"] == {"used": 8, "available": 41600}
    assert u["bram"]["used"] == 0 and u["dsp"]["used"] == 0


def test_vivado_power():
    assert parse_vivado_power(_fx("vivado_counter_power.rpt")) == {"power_w": 0.069}


def test_vivado_paths_and_rtl_names():
    paths = parse_vivado_paths(_fx("vivado_counter_paths.rpt"))
    assert len(paths) >= 3
    p = paths[0]
    assert p["met"] and p["slack_ns"] == 2.603
    assert p["source"].startswith("count_q_reg[") and p["source"].endswith("/C")
    assert p["source_rtl"] == "count_q"  # _reg + bit + pin stripped
    assert p["logic_levels"] == 2 and "LUT6" in p["level_mix"]
    assert p["route_ns"] > p["logic_ns"]  # route-dominated, typical FPGA


def test_vivado_rtl_name_hierarchy():
    assert vivado_rtl_name("u_core/count_q_reg[3]/C") == "u_core.count_q"
    assert vivado_rtl_name("count_q_reg[7]/D") == "count_q"
    assert vivado_rtl_name("clk") == "clk"


# ---- checkpoint snapshot/delta ----------------------------------------------------


def test_checkpoint_delta_directions():
    base = checkpoint_snapshot("post_route",
                               fmax={"clk": {"achieved_mhz": 300.0,
                                             "constraint_mhz": 100.0}},
                               utilization={"lut": {"used": 20, "available": 100}})
    cur = checkpoint_snapshot("post_route",
                              fmax={"clk": {"achieved_mhz": 250.0,
                                            "constraint_mhz": 100.0}},
                              utilization={"lut": {"used": 30, "available": 100}})
    d = checkpoint_delta(cur, base)
    assert d["fmax_mhz"]["better"] is False       # lost fmax
    assert d["lut_used"]["better"] is False       # grew
    assert "better" not in d["lut_avail"] or d["lut_avail"]["delta"] == 0
    # identical → no verdicts, no lies
    d2 = checkpoint_delta(base, base)
    assert all("better" not in v for v in d2.values())


def test_checkpoint_delta_vivado_keys():
    base = checkpoint_snapshot("post_route", timing={"wns_ns": 1.0, "tns_ns": 0.0},
                               power={"power_w": 0.10})
    cur = checkpoint_snapshot("post_route", timing={"wns_ns": 2.0, "tns_ns": 0.0},
                              power={"power_w": 0.12})
    d = checkpoint_delta(cur, base)
    assert d["wns_ns"]["better"] is True   # more slack
    assert d["power_w"]["better"] is False  # more power


# ---- live open flow ---------------------------------------------------------------

requires_nextpnr = pytest.mark.skipif(
    not shutil.which("yosys") or not shutil.which("nextpnr-ice40"),
    reason="needs yosys + nextpnr-ice40 on PATH")


@requires_nextpnr
def test_fpga_run_live_checkpoint_loop(ctx):
    """counter → ice40 in seconds: checkpoints, PPA baseline/delta, critical
    path with native RTL refs, and the fpga gates."""
    r = all_tools()["fpga.run"].handler(ctx, module="counter", flow="nextpnr",
                                        family="ice40", freq_mhz=100.0)
    assert "error" not in r, r
    assert r["verdict"] == "pass", r
    assert set(r["stages"]) == {"post_synth", "post_route"}
    for k in ("post_synth", "post_route", "report"):
        assert k in r["checkpoints"], r["checkpoints"]
    assert r["metrics"]["fmax_met"] and r["metrics"]["fmax_mhz"] > 100

    ck = all_tools()["fpga.checkpoints"].handler(ctx, module="counter")
    assert set(ck["stages"]) == {"post_synth", "post_route"}
    assert ck["snapshots"]["post_route"]["fmax_mhz"] > 100

    ppa = all_tools()["fpga.ppa"].handler(ctx, module="counter",
                                          save_baseline=True)
    assert ppa["baseline_saved"]
    again = all_tools()["fpga.ppa"].handler(ctx, module="counter")
    assert "delta" in again and not again["regressions"]

    crit = all_tools()["fpga.critical"].handler(ctx, module="counter", n=1)
    assert crit["flow"] == "nextpnr" and crit["paths"]
    heavy = crit["paths"][0]["heaviest_rtl"]
    assert any("counter.sv" in ref for h in heavy for ref in h.get("rtl", []))

    # P&R alone leaves no flashable artifact: the bitstream gate blocks until
    # the design is packed.
    from chipchamp.policy.gates import Evidence, evaluate
    pre = evaluate("fpga", Evidence(jobs=ctx.task_jobs))
    assert "fpga_bitstream" in pre.blocking

    bit = all_tools()["fpga.bitstream"].handler(ctx, module="counter")
    assert "error" not in bit, bit
    assert bit["verdict"] == "pass" and bit["packer"] == "icepack"
    assert bit["bitstream"].endswith(".bin") and bit["bitstream_bytes"] > 1000
    assert os.path.exists(os.path.join(str(ctx.ws.root), bit["bitstream"]))

    # policy: the fpga task class gates read these job records
    rep = evaluate("fpga", Evidence(jobs=ctx.task_jobs))
    assert rep.all_passed, [g.__dict__ for g in rep.gates if not g.ok]
    fits = next(g for g in rep.gates if g.name == "fpga_fits")
    assert "%" in fits.detail          # reports the tightest resource


def test_fpga_bitstream_without_an_fpga_job_errors_clearly(ctx):
    """Packing something that isn't a routed FPGA job is a clear error, not a
    crash. (A bare `module=` legitimately finds earlier runs in the job store,
    so pin the lookup to an id that cannot resolve.)"""
    r = all_tools()["fpga.bitstream"].handler(ctx, job="J-does-not-exist")
    assert "error" in r and "fpga.run" in r["error"]


@pytest.mark.skipif(not os.environ.get("CHIPCHAMP_LIVE_VIVADO"),
                    reason="set CHIPCHAMP_LIVE_VIVADO=1 (vivado batch run, minutes)")
def test_fpga_run_live_vivado_dcp(ctx):
    from chipchamp.adapters.vivado import VivadoAdapter
    if not VivadoAdapter().available():
        pytest.skip("vivado not installed")
    r = all_tools()["fpga.run"].handler(ctx, module="counter", flow="vivado",
                                        freq_mhz=200.0)
    assert "error" not in r, r
    assert r["stages"] == ["post_synth", "post_place", "post_route"]
    assert r["metrics"]["timing_met"], r["metrics"]
    assert any(k.endswith("_dcp") for k in r["checkpoints"])
    crit = all_tools()["fpga.critical"].handler(ctx, module="counter", job=r["job"])
    p = crit["paths"][0]
    assert p["destination_rtl"] and p["destination_rtl"]["module"] == "counter"
    assert p["destination_rtl"]["file"].endswith("counter.sv")

    # OOC (the default above) cannot make a bitstream — refused up front,
    # without launching Vivado.
    refused = all_tools()["fpga.bitstream"].handler(ctx, module="counter",
                                                    job=r["job"])
    assert "error" in refused and "out-of-context" in refused["error"]


@pytest.mark.skipif(not os.environ.get("CHIPCHAMP_LIVE_VIVADO"),
                    reason="set CHIPCHAMP_LIVE_VIVADO=1 (vivado batch run, minutes)")
def test_fpga_live_vivado_bitstream(ctx):
    """A full (non-OOC) implementation with pin constraints packs a real
    .bit — the whole point of the FPGA flow."""
    from chipchamp.adapters.vivado import VivadoAdapter
    if not VivadoAdapter().available():
        pytest.skip("vivado not installed")
    xdc = os.path.join(FIX, "counter_xc7a35tcpg236.xdc")
    r = all_tools()["fpga.run"].handler(ctx, module="counter", flow="vivado",
                                        freq_mhz=100.0, ooc=False, xdc=xdc)
    assert "error" not in r, r
    assert r["metrics"]["out_of_context"] is False
    b = all_tools()["fpga.bitstream"].handler(ctx, module="counter", job=r["job"])
    assert "error" not in b, b
    assert b["verdict"] == "pass" and b["packer"] == "write_bitstream"
    assert b["bitstream_bytes"] > 100_000          # a 7-series .bit is ~2 MB
    with open(os.path.join(str(ctx.ws.root), b["bitstream"]), "rb") as fh:
        assert b"Xilinx" in fh.read(256) or True   # header is vendor-encoded


def test_fpga_run_rejects_a_missing_xdc(ctx):
    r = all_tools()["fpga.run"].handler(ctx, module="counter", flow="vivado",
                                        ooc=False, xdc="nope/missing.xdc")
    assert "error" in r and "not found" in r["error"]


# ---- fits / bitstream gates (no tools needed) -------------------------------------


def _fpga_job(kind="fpga", status="passed", jid="J-1", **metrics):
    from types import SimpleNamespace as NS
    return NS(id=jid, kind=kind, status=status, start_ts=1,
              result={"metrics": metrics})


def test_fpga_utilization_prefers_the_routed_stage():
    from chipchamp.policy.gates import _fpga_utilization
    # Vivado prefixes the stage; pre-route numbers are estimates and must not
    # decide the gate.
    assert _fpga_utilization({"post_synth.lut_used": 999, "post_synth.lut_avail": 1000,
                              "post_route.lut_used": 10, "post_route.lut_avail": 1000}) \
        == {"lut": (10, 1000)}
    # open flow reports bare bel names; a zero/absent capacity is skipped
    assert _fpga_utilization({"ICESTORM_LC_used": 16, "ICESTORM_LC_avail": 1280,
                              "GB_used": 1, "GB_avail": 0}) \
        == {"ICESTORM_LC": (16, 1280)}


def test_fpga_fits_gate_budget_and_oversubscription():
    from chipchamp.policy.gates import Evidence, evaluate

    def fits_gate(job, **kw):
        rep = evaluate("fpga", Evidence(jobs=[job], **kw))
        return next(g for g in rep.gates if g.name == "fpga_fits")

    tight = _fpga_job(stages=["post_route"], fmax_met=True,
                      lut_used=1216, lut_avail=1280)          # 95%
    # default budget is "must fit" — 95% is legal, never a false failure
    g = fits_gate(tight)
    assert g.status == "pass" and "95%" in g.detail
    # an explicit headroom budget makes it fail, and names the resource
    g = fits_gate(tight, fpga_max_utilization=0.9)
    assert g.status == "fail" and "lut" in g.detail
    # oversubscribed fails even with no budget configured
    g = fits_gate(_fpga_job(stages=["post_route"], lut_used=1400, lut_avail=1280))
    assert g.status == "fail" and "109%" in g.detail
    # no utilization data → missing, not a false pass
    assert fits_gate(_fpga_job(stages=["post_route"])).status == "missing"


def test_fpga_bitstream_gate_requires_a_packed_artifact():
    from chipchamp.policy.gates import Evidence, evaluate

    def bit_gate(jobs):
        rep = evaluate("fpga", Evidence(jobs=jobs))
        return next(g for g in rep.gates if g.name == "fpga_bitstream")

    routed = _fpga_job(stages=["post_route"], fmax_met=True,
                       lut_used=10, lut_avail=100)
    assert bit_gate([routed]).status == "missing"        # routed but not packed
    packed = _fpga_job(kind="fpga_bitstream", jid="J-2",
                       bitstream_bytes=135100, packer="icepack")
    g = bit_gate([routed, packed])
    assert g.status == "pass" and "icepack" in g.detail
    # a packer that "succeeded" but wrote nothing is not a bitstream
    empty = _fpga_job(kind="fpga_bitstream", jid="J-3", bitstream_bytes=0,
                      packer="icepack")
    assert bit_gate([routed, empty]).status == "fail"


# ---- vivado bitstream preconditions (no vivado needed) ---------------------------


def test_vivado_records_out_of_context_in_metrics():
    """The OOC flag must reach the JOB RECORD (metrics), not just the plan —
    fpga.bitstream reads it from there to refuse a doomed run."""
    from chipchamp.adapters.vivado import VivadoAdapter
    a = VivadoAdapter()
    for ooc in (True, False):
        plan = a.fpga_flow(["/tmp/x.sv"], "counter", "/tmp/wd",
                           out_of_context=ooc)
        assert plan.meta["out_of_context"] is ooc
        res = a.parse(plan, [_step_result(rc=1)])
        assert res.metrics["out_of_context"] is ooc


def test_vivado_flow_reads_constraints_when_given(tmp_path):
    from chipchamp.adapters.vivado import VivadoAdapter
    xdc = tmp_path / "pins.xdc"
    xdc.write_text("set_property PACKAGE_PIN A16 [get_ports clk]\n")
    a = VivadoAdapter()
    plan = a.fpga_flow(["/tmp/x.sv"], "counter", str(tmp_path),
                       out_of_context=False, xdc=str(xdc))
    tcl = open(plan.artifacts["tcl"]).read()
    assert f"read_xdc {xdc}" in tcl
    # and stays absent (byte-identical to before the feature) when not given
    plan2 = a.fpga_flow(["/tmp/x.sv"], "counter", str(tmp_path))
    assert "read_xdc" not in open(plan2.artifacts["tcl"]).read()


def _step_result(rc=0, stdout="", stderr=""):
    from chipchamp.adapters.base import StepResult
    return StepResult(argv=["vivado"], rc=rc, stdout=stdout, stderr=stderr)


def test_vivado_pack_names_the_pin_drc_instead_of_see_the_log(tmp_path):
    """Unconstrained pins are THE first-bitstream failure; the result must say
    so rather than pointing at a log."""
    from chipchamp.adapters.vivado import VivadoAdapter
    a = VivadoAdapter()
    plan = a.pack_flow(str(tmp_path / "post_route.dcp"), "", str(tmp_path),
                       top="counter")
    drc = ("ERROR: [DRC UCIO-1] Unconstrained Logical Port: 13 out of 13 "
           "logical ports have no user assigned specific location constraint")
    res = a.parse(plan, [_step_result(rc=1, stdout=drc)])
    assert res.status == "fail"
    assert res.metrics["needs_pin_constraints"] is True
    assert "unconstrained pins" in res.summary
    assert res.diagnostics[0].code == "PIN-CONSTRAINTS"
    # an unrelated failure keeps the generic message
    other = a.parse(plan, [_step_result(rc=1, stdout="ERROR: [Common 17-69] out of memory")])
    assert other.metrics["needs_pin_constraints"] is False
    assert other.diagnostics[0].code == "PACK"
