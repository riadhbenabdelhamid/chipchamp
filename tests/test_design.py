"""Design intelligence: parser, database, hierarchy, domains, FSM, cone, diff."""
from __future__ import annotations

from chipchamp.design import DesignDB
from chipchamp.design.parser import parse_source

COUNTER = """
module counter #(parameter int W=8) (
  input logic clk, input logic rst_n, input logic en,
  output logic [W-1:0] q);
  logic [W-1:0] q_r;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) q_r <= '0; else if (en) q_r <= q_r + 1'b1;
  assign q = q_r;
endmodule
"""


def test_parse_ports_params():
    m = parse_source(COUNTER, "c.sv")[0]
    assert m.name == "counter"
    assert m.param("W").default_expr == "8"
    dirs = {p.name: p.direction for p in m.ports}
    assert dirs == {"clk": "input", "rst_n": "input", "en": "input", "q": "output"}
    assert m.port("q").width_expr and "W" in m.port("q").width_expr


def test_clock_reset_inference():
    m = parse_source(COUNTER, "c.sv")[0]
    blk = m.always_blocks[0]
    assert blk.clock == "clk"
    assert "rst_n" in blk.resets
    assert "q_r" in blk.lhs


def test_no_leaked_number_literals():
    m = parse_source(COUNTER, "c.sv")[0]
    # 1'b1 must not leak a "b1" pseudo-signal
    assert "b1" not in m.always_blocks[0].rhs


def test_hierarchy_and_param_resolution(example_root):
    import glob
    db = DesignDB(example_root)
    for f in sorted(glob.glob(f"{example_root}/rtl/*.sv")):
        db.add_file(f)
    db.build()
    assert db.tops() == ["soc_top", "tick_timer"]
    node = db.elaborate("soc_top")
    fifo = next(c for c in node.children if c.inst_name == "u_fifo")
    assert fifo.params["WIDTH"] == "32"
    assert fifo.params["DEPTH"] == "8"
    assert fifo.params["AW"] == "3"  # $clog2(8)


def test_fsm_extraction(workspace):
    db = workspace.db()
    fsms = db.fsms.get("arbiter_fsm")
    assert fsms and set(fsms[0].states) == {"IDLE", "SERVE0", "SERVE1"}
    assert any(t[1] == "SERVE0" for t in fsms[0].transitions)


def test_cone_crosses_instance_boundary(workspace):
    db = workspace.db()
    cone = db.cone("soc_top", "beats", "fanin", 2)
    assert cone is not None
    assert any("u_beats" in s for s in cone.statements)


def test_semantic_diff(example_root):
    import glob
    a = DesignDB(example_root)
    b = DesignDB(example_root)
    for f in sorted(glob.glob(f"{example_root}/rtl/*.sv")):
        a.add_file(f)
        b.add_file(f)
    a.build()
    b.build()
    # mutate a port on one side
    b.modules["counter"].ports = b.modules["counter"].ports[:-1]
    diff = a.semantic_diff(b)
    assert any(c["module"] == "counter" for c in diff["port_changes"])


def test_persistence_roundtrip(workspace, tmp_path):
    db = workspace.db()
    p = str(tmp_path / "db.json")
    db.save(p)
    db2 = DesignDB.load(p)
    assert set(db2.modules) == set(db.modules)
    assert db2.tree_rev == db.tree_rev
