"""Reuse-loop fixes: directory lint paths, subset-lint dependency resolution,
and yosys synth of components that need package ordering + SVA-compile-out."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipchamp.adapters.yosys import YosysAdapter
from chipchamp.tools import all_tools


# ---- yosys file ordering (fix #3) ----------------------------------------------


def test_yosys_orders_packages_first_and_drops_headers(tmp_path):
    pkg = tmp_path / "my_pkg.sv"
    pkg.write_text("package my_pkg;\n  function int f(); return 1; endfunction\nendpackage\n")
    user = tmp_path / "a_user.sv"   # alphabetically BEFORE my_pkg
    user.write_text("module a_user; endmodule\n")
    hdr = tmp_path / "defs.svh"
    hdr.write_text("`define X 1\n")
    ordered = YosysAdapter._order_for_yosys([str(user), str(hdr), str(pkg)])
    assert ordered == [str(pkg), str(user)]  # package first, header dropped
    assert not any(f.endswith(".svh") for f in ordered)


def test_yosys_read_prefix_defines_synthesis():
    pre = YosysAdapter._read_prefix(["/inc"], {"FOO": 2})
    assert "-I/inc" in pre
    assert "-DSYNTHESIS=1" in pre   # compiles out library SVA by default
    assert "-DFOO=2" in pre


# ---- live reuse: fetch a component, then lint/synth (needs the library) --------


LIB = Path("/home/riadh/rtllib-pillars")


def _ws_with_fetched_fifo(tmp_path):
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True)
    (dot / "config.toml").write_text(
        f"[project]\nname='t'\n[library]\ndirs=['{LIB}']\n")
    (tmp_path / "work" / "rtl").mkdir(parents=True)
    ws = Workspace(str(tmp_path))
    ctx = ToolContext(ws)
    all_tools()["lib.fetch"].handler(ctx, name="fifo_sync", dest="work/rtl/lib")
    (tmp_path / "work" / "rtl" / "wrap.sv").write_text(
        "module wrap #(parameter int W=8, D=16)(input logic clk_i, rst_ni);\n"
        "  logic vi,ro,vo,ri; logic [W-1:0] di,dob; logic [$clog2(D):0] lv; logic fu,em;\n"
        "  fifo_sync #(.Width(W),.Depth(D)) u(.clk_i,.rst_ni,.wr_valid_i(vi),\n"
        "    .wr_ready_o(ro),.wr_data_i(di),.rd_valid_o(vo),.rd_ready_i(ri),\n"
        "    .rd_data_o(dob),.level_o(lv),.full_o(fu),.empty_o(em));\nendmodule\n")
    ws.invalidate()
    return ctx


def _has(tool):
    import shutil
    return shutil.which(tool) is not None


@pytest.mark.skipif(not (LIB / "manifest.json").is_file() or not _has("verilator"),
                    reason="needs ~/rtllib-pillars + verilator")
def test_directory_lint_path_and_subset_resolve(tmp_path):
    ctx = _ws_with_fetched_fifo(tmp_path)
    lint = all_tools()["lint.run"].handler
    # fix #1: a DIRECTORY path expands to its HDL files (verilator can't read a dir)
    d = lint(ctx, paths=["work/rtl"])
    assert "error" not in d and d["status"] == "passed" and d["errors"] == 0
    # fix #2: linting ONLY the top file still resolves the fetched fifo_sync +
    # its rtllib_pillars_pkg import (whole target compiled, report filtered)
    s = lint(ctx, paths=["work/rtl/wrap.sv"])
    assert "error" not in s and s["status"] == "passed" and s["errors"] == 0
    # every reported diagnostic is for the requested file only
    assert all("wrap.sv" in x.get("file", "") for x in s["diagnostics"])


@pytest.mark.skipif(not (LIB / "manifest.json").is_file() or not _has("yosys"),
                    reason="needs ~/rtllib-pillars + yosys")
def test_synth_reused_component(tmp_path):
    # fix #3: yosys synth of a design reusing fifo_sync (package ordering + the
    # SYNTHESIS define to compile out the library's SVA) must succeed
    ctx = _ws_with_fetched_fifo(tmp_path)
    r = all_tools()["synth.run"].handler(ctx, top="wrap")
    assert r.get("status") == "passed", r
    assert r["metrics"].get("cells", 0) > 0
