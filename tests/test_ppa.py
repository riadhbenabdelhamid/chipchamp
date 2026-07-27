"""PPA feedback loop: OpenROAD path-report parsing + gate-netlist→RTL
cross-probe. Fixtures are REAL LibreLane output (counter run, sky130)."""
from __future__ import annotations

import os

import pytest

from chipchamp.physical import GateNetlist, parse_path_report, rtl_net_of

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIX, name), "r") as fh:
        return fh.read()


def test_parse_real_openroad_report():
    paths = parse_path_report(_fixture("librelane_counter_max.rpt"))
    assert len(paths) == 25
    p = paths[0]
    assert p.startpoint == "en" and "input port" in p.startpoint_kind
    assert p.endpoint == "tc" and "output port" in p.endpoint_kind
    assert p.group == "clk" and p.path_type == "max"
    assert p.corner == "nom_ss_100C_1v60"  # from the corner banner
    assert p.met and abs(p.slack - 1.691415) < 1e-6
    assert abs(p.arrival - 6.058585) < 1e-6
    # through-points carry pin/cell/delay
    pins = [pt["pin"] for pt in p.points]
    assert "input2/X" in pins and "_25_/X" in pins
    worst = p.worst_stages(1)[0]
    assert worst["delay"] > 0.7  # the fanout13 buffer stage


def test_ff_endpoints_and_stage_ranking():
    paths = parse_path_report(_fixture("librelane_counter_max.rpt"))
    ff = [p for p in paths if "flip-flop" in p.endpoint_kind]
    assert ff, "report has FF endpoints"
    assert any(p.endpoint.startswith("_5") for p in ff)  # _47_.._54_ regs
    for p in paths:
        assert p.points, f"path {p.startpoint}->{p.endpoint} has no stages"
        stages = p.worst_stages(3)
        assert stages == sorted(stages, key=lambda s: -s["delay"])


def test_netlist_maps_anon_ff_to_rtl_net():
    nl = GateNetlist.parse(_fixture("librelane_counter_synth_nl.v"))
    assert "_54_" in nl.insts
    ff = nl.insts["_54_"]
    assert ff.is_seq and ff.cell == "sky130_fd_sc_hd__dfrtp_2"
    # the whole point: _54_ is really count[7]
    assert rtl_net_of("_54_", nl) == "count[7]"
    assert rtl_net_of("_54_/D", nl) == "count[7]"
    # ports pass through; anonymous nets refuse to pretend
    assert rtl_net_of("en", nl) == "en"
    assert rtl_net_of("_25_", nl) is None  # and-gate driving anon net


def test_resolve_hier_flat_and_bitselect(ctx):
    from chipchamp.physical import resolve_hier
    r = resolve_hier(ctx.db, "counter", "count[7]")
    assert r and r["module"] == "counter" and r["signal"] == "count"
    assert r["file"].endswith("counter.sv") and r["line"] > 0


def test_resolve_hier_through_instance(ctx):
    """soc_top instantiates counter as u_count — hierarchical names resolve."""
    from chipchamp.physical import resolve_hier
    node = ctx.db.elaborate("soc_top")
    inst = next((c.inst_name for c in node.children if c.module == "counter"), None)
    if inst is None:
        pytest.skip("example hierarchy changed")
    r = resolve_hier(ctx.db, "soc_top", f"{inst}.count[3]")
    assert r and r["module"] == "counter" and r["signal"] == "count"


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "examples",
                                   "soc", ".chipchamp", "pd", "counter", "runs")),
    reason="needs the on-disk LibreLane counter run")
def test_pd_critical_live_on_kept_run(ctx):
    """End-to-end over the real kept run: worst path lands on RTL file:line."""
    from chipchamp.tools import all_tools
    r = all_tools()["pd.critical"].handler(ctx, module="counter", n=2)
    assert "error" not in r, r
    assert r["corners"] and r["paths"]
    p = r["paths"][0]
    assert p["slack_ns"] == r["wns_ns"]
    # at least one end of the worst path cross-probes to RTL
    probed = [d for d in (p["startpoint"], p["endpoint"]) if "rtl" in d]
    assert probed, p
    rtl = probed[0]["rtl"]
    assert rtl["module"] == "counter" and rtl["file"].endswith("counter.sv")
    assert probed[0].get("cone"), "fan-in cone missing"
