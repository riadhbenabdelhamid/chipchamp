"""Production SV front-end (slang): the cases the pragmatic parser fails on now
parse correctly, and the DesignDB uses slang end-to-end."""
from __future__ import annotations

import glob

import pytest

from chipchamp.design import slang_frontend as sf
from chipchamp.design.parser import parse_source

pytestmark = pytest.mark.skipif(not sf.available(), reason="pyslang not installed")

GNARLY = """
package my_pkg; typedef enum logic[1:0]{IDLE=2'b00,RUN=2'b01,DONE=2'b10} st_t; endpackage
module blk import my_pkg::*; #(parameter int WIDTH=8)
  (input logic clk, input logic rst_n, input logic en,
   output logic [WIDTH-1:0] count, output logic tc);
  logic [WIDTH-1:0] q; st_t state;
  always_ff @(posedge clk or negedge rst_n) if(!rst_n) q<='0; else if(en) q<=q+1'b1;
  assign count = q; assign tc = &q;
endmodule
module wrap #(parameter int N=4)(input logic clk, output logic [7:0] o);
  genvar i;
  blk #(.WIDTH(8)) u_b (.clk(clk), .rst_n(1'b1), .en(1'b1), .count(o), .tc());
endmodule
"""


def _compile(tmp_path, text):
    f = tmp_path / "g.sv"
    f.write_text(text)
    return sf.compile_design([str(f)])


def test_slang_gets_what_regex_misses(tmp_path):
    mods = _compile(tmp_path, GNARLY)
    # the pragmatic parser returns EMPTY ports here (import-in-header + width expr)
    prag = {m.name: m for m in parse_source(GNARLY, "g.sv")}
    assert len(prag["blk"].ports) == 0            # regex parser fails
    # slang gets all 5, with resolved widths
    blk = mods["blk"]
    assert [p.name for p in blk.ports] == ["clk", "rst_n", "en", "count", "tc"]
    assert blk.port("count").width_expr == "[7:0]"    # WIDTH resolved to 8
    assert blk.param("WIDTH").default_expr == "8"


def test_slang_instance_overrides_and_connections(tmp_path):
    wrap = _compile(tmp_path, GNARLY)["wrap"]
    inst = wrap.instances[0]
    assert inst.module_type == "blk"
    assert inst.param_overrides.get("WIDTH") == "8"
    assert inst.connections["count"] == "o"           # output net recovered
    assert inst.connections["clk"] == "clk"


def test_slang_module_local_enum_and_fsm(workspace):
    db = workspace.db(rebuild=True)
    assert db.frontend == "slang"
    fsms = db.fsms.get("arbiter_fsm")
    assert fsms and set(fsms[0].states) == {"IDLE", "SERVE0", "SERVE1"}


def test_db_hierarchy_and_domains_via_slang(workspace):
    db = workspace.db()
    assert db.frontend == "slang"
    node = db.elaborate("soc_top")
    fifo = next(c for c in node.children if c.inst_name == "u_fifo")
    assert fifo.params["WIDTH"] == "32" and fifo.params["AW"] == "3"
    assert "clk_core" in db.domain_report("soc_top").clocks


def test_slang_handles_all_example_rtl():
    mods = sf.compile_design(sorted(glob.glob("examples/soc/rtl/*.sv")))
    assert {"counter", "sync_fifo", "arbiter_fsm", "soc_top"} <= set(mods)
    # every module has high confidence (no silent degradation)
    assert all(m.parse_confidence == "high" for m in mods.values())
