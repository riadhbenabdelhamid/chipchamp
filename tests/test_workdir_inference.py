"""Inferred-target scoping + agent work dir (the launch-in-my-repo autopsy fix).

Regression for the session where launching from a repo whose only HDL was the
bundled examples/**/verif/uvm swept 25 uncompilable UVM files into the target,
so every sim/synth failed on uvm_macros.svh."""
from __future__ import annotations

import os
from pathlib import Path

from chipchamp.config import (Workspace, is_testbench, rtl_only, _glob_rtl,  # noqa: F401
                             WORK_DIRNAME)


def test_inference_excludes_bundled_examples(tmp_path):
    # a repo whose only HDL is under examples/ (with UVM) — the autopsy shape
    uvm = tmp_path / "examples" / "soc" / "verif" / "uvm"
    uvm.mkdir(parents=True)
    (uvm / "counter_pkg.sv").write_text('`include "uvm_macros.svh"\npackage p; endpackage')
    (tmp_path / "examples" / "soc" / "rtl").mkdir(parents=True)
    (tmp_path / "examples" / "soc" / "rtl" / "counter.sv").write_text("module counter; endmodule")

    ws = Workspace(str(tmp_path))
    srcs = ws.target(ws.default_target).sources
    assert srcs == [], "examples/ must not be swept into an inferred target"


def test_work_dir_is_globbed_live(tmp_path):
    ws = Workspace(str(tmp_path))
    t = ws.target(ws.default_target)
    assert t.sources == []
    # the agent writes a design into the work dir mid-session
    ws.ensure_work_dir()
    (ws.work_dir / "rtl").mkdir()
    (ws.work_dir / "rtl" / "d.sv").write_text("module d; endmodule")
    ws.invalidate()  # what fs.write does after a write; no workspace rebuild
    assert any(s.endswith("d.sv") for s in t.sources)
    assert ws.work_dir == tmp_path / WORK_DIRNAME


def test_conventional_rtl_dir_still_works(tmp_path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.sv").write_text("module top; endmodule")
    ws = Workspace(str(tmp_path))
    assert any(s.endswith("top.sv") for s in ws.target(ws.default_target).sources)


def test_odd_layout_fallback_recursive_but_filtered(tmp_path):
    # HDL in a non-conventional dir -> recursive fallback finds it, but still
    # skips vendored/examples
    (tmp_path / "cores" / "alu").mkdir(parents=True)
    (tmp_path / "cores" / "alu" / "alu.sv").write_text("module alu; endmodule")
    (tmp_path / "vendor" / "x").mkdir(parents=True)
    (tmp_path / "vendor" / "x" / "junk.sv").write_text("module junk; endmodule")
    ws = Workspace(str(tmp_path))
    srcs = ws.target(ws.default_target).sources
    assert any("alu.sv" in s for s in srcs)
    assert not any("junk.sv" in s for s in srcs)  # vendor/ skipped


def test_work_dir_override_via_config(tmp_path):
    dot = tmp_path / ".chipchamp"
    dot.mkdir()
    (dot / "config.toml").write_text(
        "[project]\nname='t'\nwork_dir='scratch'\n[targets.t]\ntop='x'\nglobs=['rtl/*.sv']\n")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "x.sv").write_text("module x; endmodule")
    ws = Workspace(str(tmp_path))
    assert ws.work_dir == tmp_path / "scratch"


def test_testbench_detection_and_rtl_only():
    assert is_testbench("examples/soc/verif/uvm/counter_pkg.sv")  # verif/uvm dir
    assert is_testbench("tb/tb_counter.sv")
    assert is_testbench("rtl/foo_tb.sv")
    assert is_testbench("sim/test_counter.sv")  # via test_ prefix, not the dir
    assert not is_testbench("rtl/counter.sv")
    assert not is_testbench("work/rtl/complex_mult.sv")
    # generic dir names must NOT be treated as testbench (they hold real RTL)
    assert not is_testbench("sim/adder.sv")
    assert not is_testbench("test/alu.sv")
    assert not is_testbench("tests/core.sv")
    assert not is_testbench("rtl/depth.sv")  # was falsely caught by the _th suffix
    mixed = ["rtl/a.sv", "tb/tb_a.sv", "verif/uvm/env.sv", "rtl/b.sv"]
    assert rtl_only(mixed) == ["rtl/a.sv", "rtl/b.sv"]


def test_live_sources_are_cached_and_invalidated(tmp_path):
    # inferred target with a work dir: .sources caches the glob and is only
    # re-read after ws.invalidate (so hot-path reads don't re-glob every time)
    ws = Workspace(str(tmp_path))  # init caches an empty live glob (no files yet)
    t = ws.target(ws.default_target)
    ws.ensure_work_dir()
    (ws.work_dir / "rtl").mkdir()
    (ws.work_dir / "rtl" / "a.sv").write_text("module a; endmodule")
    ws.invalidate()
    assert any(s.endswith("a.sv") for s in t.sources)  # re-globs, caches [a]
    # add a second file WITHOUT invalidating — cache still serves the old set
    (ws.work_dir / "rtl" / "b.sv").write_text("module b; endmodule")
    assert not any(s.endswith("b.sv") for s in t.sources)
    ws.invalidate()  # what fs.write/fs.delete/lib.fetch call after a write
    assert any(s.endswith("b.sv") for s in t.sources)


def test_glob_rtl_skips_noise(tmp_path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "good.sv").write_text("module good; endmodule")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.sv").write_text("module bad; endmodule")
    found = _glob_rtl([tmp_path])
    assert any("good.sv" in f for f in found)
    assert not any("bad.sv" in f for f in found)


def test_svh_dirs_become_incdirs(tmp_path):
    # a fetched component drops its `include "registers.svh"` alongside it; the
    # directory holding a .svh header must become an include dir so it resolves
    ws = Workspace(str(tmp_path))
    t = ws.target(ws.default_target)
    ws.ensure_work_dir()
    (ws.work_dir / "rtl" / "lib").mkdir(parents=True)
    (ws.work_dir / "rtl" / "lib" / "registers.svh").write_text("`define M 1\n")
    (ws.work_dir / "rtl" / "top.sv").write_text("module top; endmodule")  # no header
    ws.invalidate()
    incs = t.incdirs
    assert any(i.replace(os.sep, "/").endswith("work/rtl/lib") for i in incs)
    # a dir with only .sv (no header) is not an include dir
    assert not any(i.replace(os.sep, "/").endswith("work/rtl") for i in incs)


def test_rtl_dir_created_mid_session_joins_the_target(tmp_path):
    # roots used to be existence-filtered at LOAD time, so an rtl/ created
    # mid-session (e.g. by a lib.fetch) stayed invisible until relaunch
    ws = Workspace(str(tmp_path))
    t = ws.target(ws.default_target)
    assert t.sources == []
    (tmp_path / "rtl" / "lib").mkdir(parents=True)
    (tmp_path / "rtl" / "lib" / "fifo.sv").write_text("module fifo; endmodule")
    ws.invalidate()
    assert any(s.endswith("fifo.sv") for s in t.sources)
