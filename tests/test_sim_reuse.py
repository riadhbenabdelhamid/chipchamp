"""System-level simulation reuse: expose + fetch a component's C++ golden
reference model, and run a composed Verilator C++ harness (verilator_cpp)."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from chipchamp.brand import marker

import pytest

from chipchamp import library as lib
from chipchamp.adapters.verilator import VerilatorAdapter
from chipchamp.tools import all_tools


def _mk_ws(tmp_path, config_text):
    from chipchamp.config import Workspace
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "config.toml").write_text(config_text)
    (tmp_path / "work" / "rtl").mkdir(parents=True, exist_ok=True)
    return Workspace(str(tmp_path))


def _ctx(ws):
    from chipchamp.tools.context import ToolContext
    return ToolContext(ws)


def _mk_lib_with_ref(root: Path) -> Path:
    (root / "common" / "verif" / "verilator").mkdir(parents=True)
    (root / "common" / "verif" / "verilator" / "tb_base.hpp").write_text("// base\n")
    d = root / "storage" / "widget"
    (d / "rtl").mkdir(parents=True)
    (d / "rtl" / "widget.sv").write_text("module widget; endmodule\n")
    (d / "tb" / "verilator").mkdir(parents=True)
    (d / "tb" / "verilator" / "ref_widget.hpp").write_text("class RefWidget{};\n")
    manifest = {"library": "mini", "modules": [{
        "name": "widget", "category": "storage/widget", "summary": "w",
        "top": "widget", "language": "systemverilog",
        "verification": {"ref_model": "tb/verilator/ref_widget.hpp",
                         "sv_tb": "tb/sv/widget_tb.sv"}}]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


# ---- verification metadata + fetch (unit) --------------------------------------


def test_verification_collateral_resolves_and_flags_existence(tmp_path):
    root = _mk_lib_with_ref(tmp_path / "mini")
    ws = _mk_ws(tmp_path / "w", f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    mod = lib.discover(ws)["widget"]
    v = lib.verification_collateral(mod)
    assert v["ref_model"]["exists"] is True
    assert v["ref_model"]["path"] == "tb/verilator/ref_widget.hpp"
    assert v["sv_tb"]["exists"] is False   # declared but not on disk


def test_sim_ref_files_are_ref_model_plus_harness_base(tmp_path):
    root = _mk_lib_with_ref(tmp_path / "mini")
    ws = _mk_ws(tmp_path / "w", f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    mod = lib.discover(ws)["widget"]
    rels = [f["rel"] for f in lib.sim_ref_files(mod)]
    assert rels == ["ref_widget.hpp", "tb_base.hpp"]


def test_lib_fetch_sim_view_writes_ref_model(tmp_path):
    root = _mk_lib_with_ref(tmp_path / "mini")
    ws = _mk_ws(tmp_path / "w", f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    ctx = _ctx(ws)
    res = all_tools()["lib.fetch"].handler(
        ctx, name="widget", view="sim", dest="work/tb/verilator")
    assert sorted(res["written"]) == [
        "work/tb/verilator/ref_widget.hpp", "work/tb/verilator/tb_base.hpp"]
    assert "verilator_cpp" in res["note"] and marker("PASS") in res["note"]
    assert (Path(ws.root) / "work/tb/verilator/ref_widget.hpp").is_file()
    # rtl view still works and is distinct
    r2 = all_tools()["lib.fetch"].handler(ctx, name="widget", view="rtl",
                                          dest="work/rtl/lib")
    assert any("widget.sv" in w for w in r2["written"])
    assert "instantiation" in r2


def test_lib_fetch_sim_view_errors_without_ref(tmp_path):
    # a component with no ref_model
    root = tmp_path / "mini2"
    (root / "storage" / "plain" / "rtl").mkdir(parents=True)
    (root / "storage" / "plain" / "rtl" / "plain.sv").write_text("module plain; endmodule")
    (root / "manifest.json").write_text(json.dumps({"library": "m", "modules": [
        {"name": "plain", "category": "storage/plain", "summary": "p", "top": "plain"}]}))
    ws = _mk_ws(tmp_path / "w2", f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    res = all_tools()["lib.fetch"].handler(_ctx(ws), name="plain", view="sim")
    assert "error" in res and "ref_" in res["error"]


def test_invalidate_reloads_tests_authored_mid_session(tmp_path):
    """An agent that writes a new tests.yaml entry mid-session (then invalidates,
    as every fs write does) must be able to run it — find_test reads ws.tests,
    which is only reloaded on invalidate()."""
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    ctx = _ctx(ws)
    assert ctx.find_test("late") is None
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(
        "tests:\n  - name: late\n    runner: {kind: verilator_cpp, top: x}\n")
    ws.invalidate()
    assert (ctx.find_test("late") or {}).get("runner", {})["kind"] == "verilator_cpp"


def test_runner_of_accepts_flattened_and_nested():
    """A verilator_cpp test whose runner fields are FLATTENED to the top level
    (kind/cpp/top not nested under `runner:`) must still resolve — otherwise it
    silently falls back to the iverilog sim and fails to compile the DUT."""
    from chipchamp.tools.verif_tools import _runner_of
    nested = {"name": "t", "runner": {"kind": "verilator_cpp", "cpp": ["h.cpp"]}}
    flat = {"name": "t", "kind": "verilator_cpp", "cpp": ["h.cpp"], "top": "x"}
    assert _runner_of(nested)["kind"] == "verilator_cpp"
    assert _runner_of(flat)["kind"] == "verilator_cpp"      # the bug qwen hit
    assert _runner_of(flat)["cpp"] == ["h.cpp"]
    assert _runner_of({"name": "t"}).get("kind") is None
    assert _runner_of(None) == {}


@pytest.mark.skipif(not shutil.which("verilator"), reason="needs verilator")
def test_flattened_verilator_cpp_routes_to_verilator(tmp_path):
    from chipchamp.tools.verif_tools import _plan_for_test
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    (Path(ws.root) / "work/rtl/x.sv").write_text("module x(input logic clk_i); endmodule\n")
    (Path(ws.root) / "work/tb").mkdir(parents=True, exist_ok=True)
    (Path(ws.root) / "work/tb/h.cpp").write_text("int main(){return 0;}\n")
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(  # FLATTENED, no runner:
        "tests:\n  - name: t\n    kind: verilator_cpp\n    top: x\n"
        "    cpp: [work/tb/h.cpp]\n    cpp_incdirs: [work/tb]\n")
    ws.invalidate()
    got = _plan_for_test(_ctx(ws), "t")
    assert not isinstance(got, dict), got          # not an error
    adapter, plan, files, timeout = got
    assert adapter.name == "verilator" and plan.meta["mode"] == "cpp"


# ---- verilator sim_cpp plan (unit) ---------------------------------------------


def test_sim_cpp_plan_shape():
    a = VerilatorAdapter()
    plan = a.sim_cpp(["/rtl/top.sv"], ["/tb/h.cpp"], "top", "/ws",
                     cpp_incdirs=["/tb"], incdirs=["/rtl/inc"], defines={"D": 1})
    build = plan.steps[0].argv
    for flag in ("--cc", "--exe", "--build", "-CFLAGS"):
        assert flag in build
    cflags = build[build.index("-CFLAGS") + 1]
    assert "-I/tb" in cflags and "-std=c++17" in cflags
    assert "+incdir+/rtl/inc" in build and "+define+D=1" in build
    assert "/rtl/top.sv" in build and "/tb/h.cpp" in build
    assert plan.steps[1].argv[0].endswith("Vsim")   # second step runs it
    assert plan.meta["mode"] == "cpp"


# ---- live end-to-end: reuse a real component's RTL + C++ ref model --------------


LIVE = Path(os.path.expanduser("~/rtllib-pillars"))


@pytest.mark.skipif(not (LIVE / "manifest.json").is_file()
                    or not shutil.which("verilator"),
                    reason="needs ~/rtllib-pillars + verilator")
def test_live_system_sim_reuses_ref_model(tmp_path):
    ws = _mk_ws(tmp_path / "w",
                f"[project]\nname='t'\n[library]\ndirs=['{LIVE}']\n")
    ctx = _ctx(ws)
    fetch = all_tools()["lib.fetch"].handler
    fetch(ctx, name="fifo_sync", view="rtl", dest="work/rtl/lib")
    fetch(ctx, name="fifo_sync", view="sim", dest="work/tb/verilator")
    # a hole wrapper reusing the fetched fifo_sync RTL
    (Path(ws.root) / "work/rtl/sysbuf.sv").write_text(
        "module sysbuf #(parameter int unsigned Width=32, Depth=16)(\n"
        "  input logic clk_i, rst_ni,\n"
        "  input logic wr_valid_i, output logic wr_ready_o, input logic [Width-1:0] wr_data_i,\n"
        "  output logic rd_valid_o, input logic rd_ready_i, output logic [Width-1:0] rd_data_o,\n"
        "  output logic [$clog2(Depth):0] level_o, output logic full_o, empty_o);\n"
        "  fifo_sync #(.Width(Width),.Depth(Depth)) u(.clk_i,.rst_ni,.wr_valid_i,\n"
        "    .wr_ready_o,.wr_data_i,.rd_valid_o,.rd_ready_i,.rd_data_o,.level_o,.full_o,.empty_o);\n"
        "endmodule\n")
    # harness reuses ref_fifo_sync.hpp golden model
    (Path(ws.root) / "work/tb/verilator/sysbuf_tb.cpp").write_text(
        '#include "Vsysbuf.h"\n#include "tb_base.hpp"\n#include "ref_fifo_sync.hpp"\n'
        '#include <cstdio>\nstatic const unsigned DEPTH=16;\n'
        "int main(int argc,char**argv){Verilated::commandArgs(argc,argv);\n"
        " auto*dut=new Vsysbuf; rv::Sim<Vsysbuf> s(dut,[&](uint8_t v){dut->clk_i=v;},[&](uint8_t v){dut->rst_ni=v;});\n"
        " rv::Rng rng(0xF1F0C0DE); RefFifoSync ref(DEPTH);\n"
        " s.reset(4); s.dut->wr_valid_i=0; s.dut->rd_ready_i=0; s.eval();\n"
        " for(int i=0;i<20000;++i){uint8_t o=rng.chance(70),t=rng.chance(60);uint32_t d=rng.u32();\n"
        "  s.dut->wr_valid_i=o;s.dut->wr_data_i=d;s.dut->rd_ready_i=t;s.dut->eval();\n"
        '  s.check_eq(s.dut->level_o,ref.level(),"level");\n'
        "  bool dp=o&&s.dut->wr_ready_o,pp=t&&s.dut->rd_valid_o;\n"
        '  if(pp) s.check_eq(s.dut->rd_data_o,ref.front(),"rd");\n'
        "  s.tick(); if(dp)ref.push(d); if(pp)ref.pop(); if(s.errors>20)break;}\n"
        ' int rc=s.finish("SYS"); std::printf(rc==0?"CHIPCHAMP_PASS\\n":"CHIPCHAMP_FAIL\\n"); return rc;}\n')
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(
        "tests:\n  - name: sysbuf_test\n    runner: {kind: verilator_cpp, "
        "top: sysbuf, cpp: [work/tb/verilator/sysbuf_tb.cpp], "
        "cpp_incdirs: [work/tb/verilator]}\n")
    ws.invalidate()
    res = all_tools()["sim.run"].handler(ctx, test="sysbuf_test")
    assert res.get("status") == "passed", res
    assert res.get("sim_status") == "pass"


# ---- tests.yaml shape robustness (regression) ----------------------------------
# A model can hand-write tests.yaml as a bare top-level list instead of the
# canonical {tests: [...]} mapping. _load_tests runs inside ws.invalidate(),
# which fires on every fs.write/fs.edit, so a crash there used to poison the
# whole agent loop (AttributeError: 'list' object has no attribute 'get').

def _entry(name):
    return (f"- name: {name}\n  runner: {{kind: verilator_cpp, top: t, "
            "cpp: [work/tb/verilator/t.cpp], cpp_incdirs: [work/tb/verilator]}\n")


def test_load_tests_tolerates_bare_top_level_list(tmp_path):
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(_entry("bare_list"))
    ws.invalidate()  # must not raise
    assert [t["name"] for t in ws.tests] == ["bare_list"]


def test_load_tests_canonical_mapping_still_works(tmp_path):
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(
        "tests:\n  " + _entry("mapped").replace("\n", "\n  ").rstrip() + "\n")
    ws.invalidate()
    assert [t["name"] for t in ws.tests] == ["mapped"]


def test_load_tests_ignores_nonmapping_garbage(tmp_path):
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text("just a scalar string\n")
    ws.invalidate()  # must not raise
    assert ws.tests == []


def test_load_tests_tolerates_null_tests_value(tmp_path):
    # 'tests:' with a null value: dict.get's default only covers a MISSING key
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text("tests:\n")
    ws.invalidate()  # must not raise
    assert ws.tests == []          # not None — consumers iterate this


def test_load_tests_drops_non_mapping_entries(tmp_path):
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(
        "- alu_test\n- name: real\n  runner: {kind: verilator_cpp, top: t, "
        "cpp: [work/tb/verilator/t.cpp]}\n")
    ws.invalidate()
    assert [t["name"] for t in ws.tests] == ["real"]   # scalar entry dropped


def test_load_tests_tolerates_invalid_yaml(tmp_path):
    from chipchamp.config import Workspace
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    bad = 'tests:\n  - name: "unclosed\n'
    (Path(ws.root) / ".chipchamp" / "tests.yaml").write_text(bad)
    ws.invalidate()                # must not raise (fires on every fs.write)
    assert ws.tests == []
    Workspace(ws.root)             # and a fresh session must still start


def test_load_tests_clears_stale_entries_when_file_deleted(tmp_path):
    ws = _mk_ws(tmp_path / "w", "[project]\nname='t'\n")
    p = Path(ws.root) / ".chipchamp" / "tests.yaml"
    p.write_text(_entry("gone"))
    ws.invalidate()
    assert [t["name"] for t in ws.tests] == ["gone"]
    p.unlink()
    ws.invalidate()
    assert ws.tests == []


def test_uvm_append_tolerates_bare_list_tests_yaml():
    # the loader legitimizes bare-list tests.yaml; the uvm writer must too
    import yaml
    from chipchamp.tools.uvm_tools import _append_test_entry
    old = "- name: existing\n  runner: {kind: icarus, top: t}\n"
    entry = {"name": "uvm_smoke", "kind": "uvm", "top": "tb_top"}
    new = _append_test_entry(old, entry, "uvm_smoke")   # must not raise
    data = yaml.safe_load(new)
    tests = data["tests"] if isinstance(data, dict) else data
    names = [t.get("name") for t in tests if isinstance(t, dict)]
    assert "uvm_smoke" in names and "existing" in names


def test_lib_fetch_default_dest_lands_inside_the_work_dir(tmp_path):
    # the default dest must be somewhere the inferred target actually globs —
    # a root-level rtl/lib was outside it whenever ./rtl didn't exist at load,
    # so instantiations of fetched components went MODMISSING in lint
    root = _mk_lib_with_ref(tmp_path / "mini")
    ws = _mk_ws(tmp_path / "w", f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    ctx = _ctx(ws)
    res = all_tools()["lib.fetch"].handler(ctx, name="widget", view="rtl")
    assert res.get("written"), res
    assert all(p.startswith("work/rtl/lib/") for p in res["written"]), res
    assert any(s.endswith("widget.sv")
               for s in ws.target(ws.default_target).sources), \
        "fetched RTL must be part of the target's compiled sources"
    res = all_tools()["lib.fetch"].handler(ctx, name="widget", view="sim")
    assert all(p.startswith("work/tb/verilator/") for p in res["written"]), res
