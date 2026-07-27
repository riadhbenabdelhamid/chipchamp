"""STA tools (SPEC §9-H): OpenSTA report parsing (fixture) + live Yosys
structural-depth proxy + timing-gate delta wiring."""
from __future__ import annotations

from conftest import requires_yosys

from chipchamp.adapters.opensta import parse_path_report
from chipchamp.tools import all_tools

OPENSTA_REPORT = """\
Startpoint: u_dsp/acc_reg_3_ (rising edge-triggered flip-flop clocked by clk)
Endpoint: u_dsp/out_reg_7_ (rising edge-triggered flip-flop clocked by clk)
Path Group: clk
Path Type: max

   0.00   clock clk (rise edge)
   2.10   u_dsp/mult_23/Y (AND2_X1)
  10.12   data arrival time
 -10.00   data required time
  -0.12   slack (VIOLATED)

Startpoint: ctrl_reg (rising edge-triggered flip-flop clocked by clk)
Endpoint: u_fifo/wptr_reg_0_ (rising edge-triggered flip-flop clocked by clk)
   3.31   data arrival time
   0.69   slack (MET)
wns -0.12
tns -0.12
"""


def test_opensta_report_parser_fixture():
    paths = parse_path_report(OPENSTA_REPORT)
    assert len(paths) == 2
    assert paths[0]["violated"] and paths[0]["slack"] == -0.12
    assert paths[0]["startpoint"].startswith("u_dsp/acc_reg")
    assert not paths[1]["violated"] and paths[1]["slack"] == 0.69


@requires_yosys
def test_sta_run_structural_proxy_live(ctx):
    r = all_tools()["sta.run"].handler(ctx, top="arbiter_fsm")
    assert r["status"] == "passed"
    assert r["mode"] == "structural-depth"
    assert r["metrics"]["depth"] is not None and r["metrics"]["depth"] > 0


@requires_yosys
def test_sta_delta_feeds_timing_gate(ctx):
    base = all_tools()["sta.run"].handler(ctx, top="arbiter_fsm")
    cur = all_tools()["sta.run"].handler(ctx, top="arbiter_fsm",
                                         baseline_job=base["job"])
    # identical design -> zero new violations, and the gate evidence is wired
    assert cur["delta"]["new_violations"] == 0
    assert ctx.sta_delta is not None
    from chipchamp.tools.meta_tools import _collect_evidence
    ev = _collect_evidence(ctx, [])
    assert ev.sta_new_violations == 0


@requires_yosys
def test_sta_paths_tool(ctx):
    r = all_tools()["sta.run"].handler(ctx, top="sync_fifo")
    p = all_tools()["sta.paths"].handler(ctx, job=r["job"])
    assert p["mode"] == "structural-depth"
    assert p["depth"] == r["metrics"]["depth"]
