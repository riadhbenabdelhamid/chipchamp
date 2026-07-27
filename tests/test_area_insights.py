"""Area by RTL line (src attrs through mapping + cone attribution) and
dead-width (toggle coverage × flop cost). Live on yosys + sky130 liberty."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.coverage import Bin, CoverageModel
from chipchamp.physical.area import find_liberty
from chipchamp.physical.insights import (area_by_line, dead_bits,
                                        liberty_cell_areas, load_mapped_cells)
from chipchamp.tools import all_tools

requires_yosys_liberty = pytest.mark.skipif(
    not shutil.which("yosys") or find_liberty() is None,
    reason="needs yosys and a sky130 liberty")


@requires_yosys_liberty
def test_liberty_cell_area_map():
    areas = liberty_cell_areas(find_liberty())
    assert len(areas) > 300
    assert areas["sky130_fd_sc_hd__dfrtp_1"] > areas["sky130_fd_sc_hd__inv_1"]


@requires_yosys_liberty
def test_area_by_line_live(ctx):
    """counter: the always_ff at counter.sv:15 must own the flops exactly,
    and cone attribution must sweep most ABC-stripped comb cells to lines."""
    r = all_tools()["pd.area"].handler(ctx, top="counter", by_line=True)
    assert "error" not in r and "by_line" in r, r.get("by_line_note", r)
    lines = r["by_line"]
    ff_line = next((k for k in lines if k.startswith("counter.sv:15")), None)
    assert ff_line, lines.keys()
    assert lines[ff_line]["seq_area_um2"] > 100  # 8 dfrtp flops
    # cone attribution keeps the unattributed remainder small
    total_lines = sum(v["area_um2"] for v in lines.values())
    unattr = r["by_line_unattributed"]["area_um2"]
    assert unattr < 0.25 * (total_lines + unattr), (unattr, total_lines)


def test_dead_bits_pricing_and_hints():
    m = CoverageModel()
    # ctr[3:0]: bits 0-1 toggle, bits 2-3 dead (MSB-contiguous)
    for bit in range(4):
        for edge in ("0->1", "1->0"):
            m.add_bin(Bin(id=f"toggle:rtl/x.sv:9:ctr[{bit}]:{edge}:tb.dut",
                          group="toggle", point="rtl/x.sv",
                          name=f"rtl/x.sv:9", count=5 if bit < 2 else 0,
                          source="rtl/x.sv:9", kind="toggle"))
    cells = [{"name": "_1_", "type": "sky130_fd_sc_hd__dfrtp_1", "src": "",
              "out_bits": [2], "in_bits": [], "is_seq": True,
              "out_nets": ["ctr[2]"]},
             {"name": "_2_", "type": "sky130_fd_sc_hd__dfrtp_1", "src": "",
              "out_bits": [3], "in_bits": [], "is_seq": True,
              "out_nets": ["ctr[3]"]}]
    areas = {"sky130_fd_sc_hd__dfrtp_1": 25.0}
    f = dead_bits(m, cells, areas)
    assert len(f) == 1
    x = f[0]
    assert x["signal"] == "ctr" and x["width"] == 4
    assert x["dead_bits"] == [2, 3]
    assert x["wasted_area_um2"] == 50.0 and x["priced_flops"] == 2
    assert "never toggled" in x["hint"]  # MSB-contiguous → width question


def test_dead_bits_ignores_healthy_and_scalar():
    m = CoverageModel()
    for edge in ("0->1", "1->0"):
        m.add_bin(Bin(id=f"toggle:a.sv:1:ok[0]:{edge}:h", group="toggle",
                      point="a.sv", name="a.sv:1", count=3, kind="toggle"))
        m.add_bin(Bin(id=f"toggle:a.sv:2:flag:{edge}:h", group="toggle",
                      point="a.sv", name="a.sv:2", count=0, kind="toggle"))
    assert dead_bits(m) == []


@requires_yosys_liberty
@pytest.mark.skipif(not shutil.which("verilator"), reason="needs verilator")
def test_pd_deadwidth_live_finds_fifo_dead_bits(ctx):
    """The example FIFO drives wr_data with a byte-ish pattern into a 16-bit
    port — real dead bits, priced from the real netlist."""
    cov = all_tools()["cov.run"].handler(ctx, test="fifo_smoke", set="dw")
    assert "error" not in cov, cov
    all_tools()["pd.area"].handler(ctx, top="soc_top")  # mapped netlist
    r = all_tools()["pd.deadwidth"].handler(ctx, top="soc_top", set="dw")
    assert "error" not in r, r
    assert r["total_findings"] >= 1
    sigs = {f["signal"] for f in r["findings"]}
    assert any("wr_data" in s or "rd_data" in s or "data" in s for s in sigs), sigs
    top_finding = r["findings"][0]
    assert top_finding["dead"] > 0 and top_finding["source"]
