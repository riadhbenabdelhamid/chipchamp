"""Activity-driven power: OpenSTA report parsing (REAL devshell output from
the kept counter run), VCD scope discovery, and the live loop — sim waves →
annotated routed netlist → power by group + hot instances → baseline."""
from __future__ import annotations

import os

import pytest

from chipchamp.physical.power import (aggregate_by_module, find_dut_scope,
                                     parse_power_report, power_tcl)
from chipchamp.tools import all_tools

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fx(name: str) -> str:
    with open(os.path.join(FIX, name), "r") as fh:
        return fh.read()


def test_parse_real_opensta_power_report():
    rep = parse_power_report(_fx("opensta_counter_power.txt"))
    # group table (workload-annotated run of the kept counter GDS)
    assert set(rep["groups"]) == {"sequential", "combinational", "clock",
                                  "macro", "pad"}
    assert rep["total"]["total_w"] == pytest.approx(2.312623e-4, rel=1e-4)
    assert rep["clock_share_pct"] == pytest.approx(28.3, abs=0.1)
    # hottest instances: the three clock buffers lead — the story writes itself
    insts = rep["instances"]
    assert insts[0]["instance"] == "clkbuf_0_clk"
    assert {i["instance"] for i in insts[:3]} == {
        "clkbuf_0_clk", "clkbuf_1_0__f_clk", "clkbuf_1_1__f_clk"}
    assert insts[0]["pct"] == pytest.approx(10.3, abs=0.2)
    # annotation honesty: 13 vcd-annotated, 168 propagated interior pins
    ann = rep["annotation"]
    assert ann["vcd"] == 13 and ann["unannotated"] == 168
    assert 0 < ann["rate_pct"] < 100


def test_power_tcl_shapes():
    tcl = power_tcl(liberty="l.lib", netlist="n.v", top="t", clock="clk",
                    period_ns=10.0, spef="t.spef", vcd="w.vcd", scope="tb/dut")
    assert "read_power_activities -scope tb/dut -vcd w.vcd" in tcl
    assert "read_spef t.spef" in tcl
    tcl2 = power_tcl(liberty="l.lib", netlist="n.v", top="t", clock="clk",
                     period_ns=10.0)
    assert "vectorless" in tcl2 and "read_power_activities" not in tcl2


def test_find_dut_scope_on_real_vcd():
    vcd = os.path.join(os.path.dirname(__file__), "..", "examples", "soc",
                       "tb_counter.vcd")
    if not os.path.exists(vcd):
        pytest.skip("example VCD not present (run sim first)")
    assert find_dut_scope(vcd, {"clk", "rst_n", "en", "clr", "count", "tc"}) \
        == "tb_counter/dut"


def test_aggregate_by_module_prefixes():
    insts = [{"instance": "u_core/alu/_12_", "total_w": 1e-5, "pct": 10.0},
             {"instance": "u_core/alu/_13_", "total_w": 2e-5, "pct": 20.0},
             {"instance": "u_fifo/_9_", "total_w": 1e-5, "pct": 10.0}]
    mods = aggregate_by_module(insts)
    assert list(mods)[0] == "u_core/alu"
    assert mods["u_core/alu"]["total_w"] == pytest.approx(3e-5)
    assert mods["u_core/alu"]["n"] == 2


@pytest.mark.skipif(
    not (os.path.exists(os.path.expanduser(
        "~/.local/opt/librelane/librelane-devshell-x86_64.AppImage"))
        and os.path.isdir(os.path.join(os.path.dirname(__file__), "..",
                                       "examples", "soc", ".chipchamp", "pd",
                                       "counter", "runs"))),
    reason="needs the LibreLane devshell + the kept counter run")
def test_pd_power_live_workload_loop(ctx):
    """sim → waves → annotated power on the real routed counter → baseline."""
    sim = all_tools()["sim.run"].handler(ctx, test="counter_smoke", seed=1)
    assert sim["sim_status"] == "pass", sim
    r = all_tools()["pd.power"].handler(ctx, module="counter",
                                        activity_from=sim["job"],
                                        save_baseline=True)
    assert "error" not in r, r
    assert r["activity"].startswith("job ")
    assert r["total_w"] > 0 and "sequential" in r["groups"]
    assert r["clock_share_pct"] > 5  # tiny design: clock tree is material
    assert r["annotation"]["vcd"] > 0
    # hottest instances are labeled: clock tree or a real RTL net
    labeled = [h for h in r["hottest"] if h.get("rtl_net")]
    assert labeled, r["hottest"][:3]
    assert any(h.get("rtl", {}).get("file", "").endswith("counter.sv")
               or h["rtl_net"] == "(clock tree)" for h in labeled)
    assert r["baseline_saved"]
    # second run vs baseline: same workload → delta ~0, and no false verdict
    r2 = all_tools()["pd.power"].handler(ctx, module="counter",
                                         activity_from=sim["job"])
    assert "delta" in r2
    assert abs(r2["delta"]["delta_w"]) < 1e-9
    assert "better" not in r2["delta"]


@pytest.mark.skipif(
    not (os.path.exists(os.path.expanduser(
        "~/.local/opt/librelane/librelane-devshell-x86_64.AppImage"))
        and os.path.isdir(os.path.join(os.path.dirname(__file__), "..",
                                       "examples", "soc", ".chipchamp", "pd",
                                       "counter", "runs"))),
    reason="needs the LibreLane devshell + the kept counter run")
def test_glsim_full_annotation_loop(ctx):
    """Gate-level sim of the taped-out netlist: (1) it passes the RTL TB's
    self-checks; (2) its VCD annotates EVERY pin — pd.power at 100%."""
    from chipchamp.physical.power import find_cell_models
    if find_cell_models() is None:
        pytest.skip("no sky130 cell models (ciel)")
    gl = all_tools()["pd.glsim"].handler(ctx, test="counter_smoke",
                                         module="counter", seed=1)
    assert "error" not in gl, gl
    assert gl["sim_status"] == "pass", gl   # GDS netlist passes RTL checks
    assert gl.get("waves"), gl

    r = all_tools()["pd.power"].handler(ctx, module="counter",
                                        activity_from=gl["job"])
    assert "error" not in r, r
    ann = r["annotation"]
    assert ann["unannotated"] == 0, ann     # the whole point: 100%
    assert ann["rate_pct"] == 100.0
    assert ann["vcd"] > 100                 # every gate pin, not 13 boundary pins
    assert r["total_w"] > 0
