"""uvm.scaffold: deterministic bench generation, manifest registration, and —
where verilator + uvm-core exist — the generated bench compiles against the
real UVM library. Full sim run is opt-in (CHIPCHAMP_LIVE_UVM=1): the first
Verilator+UVM build takes minutes."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from chipchamp.adapters.uvm import uvm_home
from chipchamp.tools import all_tools


def test_scaffold_structure_and_determinism(ctx):
    r = all_tools()["uvm.scaffold"].handler(ctx, module="counter",
                                            out_dir="verif/uvm_t/counter",
                                            add_test=False)
    assert "error" not in r, r
    names = {os.path.basename(f["path"]) for f in r["files"]}
    assert {"counter_if.sv", "counter_seq_item.sv", "counter_driver.sv",
            "counter_monitor.sv", "counter_agent.sv", "counter_scoreboard.sv",
            "counter_env.sv", "counter_seq_lib.sv", "counter_test_lib.sv",
            "counter_pkg.sv", "tb_counter_uvm.sv", "counter_uvm.f"} <= names
    # deterministic: second run writes nothing new
    r2 = all_tools()["uvm.scaffold"].handler(ctx, module="counter",
                                             out_dir="verif/uvm_t/counter",
                                             add_test=False)
    assert [f["sha256"] for f in r2["files"]] == [f["sha256"] for f in r["files"]]

    root = ctx.ws.root
    item = open(os.path.join(root, "verif/uvm_t/counter/counter_seq_item.sv")).read()
    # stimulus fields for data inputs only — clk/rst_n filtered out
    assert "rand logic" in item and " en;" in item and " clr;" in item
    assert "clk" not in item.split("uvm_object_utils_begin")[0]
    drv = open(os.path.join(root, "verif/uvm_t/counter/counter_driver.sv")).read()
    assert "vif.en <= req.en;" in drv and "uvm_fatal" in drv
    tb = open(os.path.join(root, "verif/uvm_t/counter/tb_counter_uvm.sv")).read()
    # counter's reset is active-low rst_n: released to 1'b1
    assert "rst_n = 1'b1;" in tb and 'run_test("counter_smoke_test")' in tb
    assert ".count(dif.count)" in tb


def test_scaffold_registers_manifest_entry(ctx):
    tpath = ctx.ws.dot / "tests.yaml"
    before = tpath.read_text()
    try:
        r = all_tools()["uvm.scaffold"].handler(ctx, module="counter",
                                                out_dir="verif/uvm_t/counter")
        assert r["manifest"].startswith("tests.yaml")
        t = ctx.find_test("counter_uvm_smoke")
        assert t and t["runner"]["kind"] == "uvm"
        assert t["runner"]["uvm_test"] == "counter_smoke_test"
        # edits recorded → visible to diff/gates
        assert any(p.endswith("tests.yaml") for p in ctx.edits)
        after = tpath.read_text()
        # the append preserved the user's file verbatim (comments included) —
        # our entry rides in a marker-delimited block at the end. A prior
        # chipchamp block (e.g. from the committed demo) is replaced, so
        # compare against the file with any old block stripped.
        import re as _re
        stripped = _re.sub(
            r"(?ms)^\s*# --- chipchamp uvm: counter_uvm_smoke ---.*?"
            r"# --- end chipchamp uvm ---\n?", "", before)
        assert after.startswith(stripped.rstrip() + "\n")
        assert "# --- chipchamp uvm: counter_uvm_smoke ---" in after
        assert "cocotb-coverage export" in after  # a pre-existing comment
        # regeneration replaces the block, not duplicates it
        all_tools()["uvm.scaffold"].handler(ctx, module="counter",
                                            out_dir="verif/uvm_t/counter")
        assert tpath.read_text().count("counter_uvm_smoke ---") == 1
    finally:
        tpath.write_text(before)


requires_uvm_verilator = pytest.mark.skipif(
    not shutil.which("verilator") or uvm_home() is None,
    reason="needs verilator and uvm-core (clone to ~/.cache/chipchamp/uvm-core)")


@requires_uvm_verilator
def test_scaffold_lints_against_real_uvm(ctx, tmp_path):
    """The generated bench parses+elaborates with the real IEEE 1800.2 library."""
    all_tools()["uvm.scaffold"].handler(ctx, module="counter",
                                        out_dir="verif/uvm_t/counter",
                                        add_test=False)
    root = ctx.ws.root
    home = uvm_home()
    bench = os.path.join(root, "verif/uvm_t/counter")
    argv = ["verilator", "--lint-only", "--timing", "-sv", "-Wno-lint",
            "-Wno-fatal", "-j", "0",
            "+define+UVM_NO_DPI", "+define+UVM_REGEX_NO_DPI",
            f"+incdir+{home}/src", f"+incdir+{bench}",
            os.path.join(home, "src", "uvm_pkg.sv"),
            os.path.join(root, "rtl", "counter.sv"),
            os.path.join(bench, "counter_pkg.sv"),
            os.path.join(bench, "tb_counter_uvm.sv"),
            "--top-module", "tb_counter_uvm"]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    hard = [ln for ln in (p.stdout + p.stderr).splitlines()
            if ln.startswith("%Error")]
    assert p.returncode == 0 and not hard, "\n".join(hard[:12]) or p.stderr[-800:]


@pytest.mark.skipif(not os.environ.get("CHIPCHAMP_LIVE_UVM"),
                    reason="set CHIPCHAMP_LIVE_UVM=1 (verilator UVM build takes minutes)")
@requires_uvm_verilator
def test_scaffolded_bench_runs_live(ctx):
    """Scaffold → sim.run → UVM report verdict, all license-free."""
    all_tools()["uvm.scaffold"].handler(ctx, module="counter",
                                        out_dir="verif/uvm_t/counter")
    r = all_tools()["sim.run"].handler(ctx, test="counter_uvm_smoke", seed=1)
    assert r["sim_status"] == "pass", r
    assert r["uvm"]["test"] == "counter_smoke_test"
    assert r["uvm"]["errors"] == 0 and r["uvm"]["fatals"] == 0
