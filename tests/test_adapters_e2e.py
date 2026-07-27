"""End-to-end adapter + tool tests against the real OSS toolchain (skip if absent)."""
from __future__ import annotations

from conftest import requires_icarus, requires_verilator, requires_yosys

from chipchamp.tools import all_tools


@requires_verilator
def test_lint_runs_and_normalizes(ctx):
    r = all_tools()["lint.run"].handler(ctx, top="soc_top")
    assert r["adapter"] in ("verilator", "verible")
    assert r["errors"] == 0  # example is lint-clean of errors
    assert isinstance(r["diagnostics"], list)


@requires_icarus
def test_sim_then_wave_queries(ctx):
    s = all_tools()["sim.run"].handler(ctx, test="fifo_smoke", seed=1)
    assert s["sim_status"] == "pass"
    job = s["waves_job"]
    info = all_tools()["wave.open"].handler(ctx, run=job)
    assert info["signal_count"] > 0
    w = all_tools()["wave.when"].handler(ctx, run=job, expr="full == 1")
    assert w["time"] is not None
    lvl = all_tools()["wave.find_signals"].handler(ctx, run=job, pattern="level")["matches"][0]
    # level == 8 (binary 1000) when full for DEPTH=8
    val = all_tools()["wave.value"].handler(ctx, run=job, signal=lvl, time=w["time"])["value"]
    assert int(val, 2) == 8


@requires_yosys
def test_synth_snapshot(ctx):
    r = all_tools()["synth.run"].handler(ctx, top="arbiter_fsm")
    assert r["metrics"].get("cells", 0) > 0
    assert r["metrics"].get("latches", -1) == 0  # clean FSM, no latches


@requires_icarus
def test_job_repro_recorded(ctx):
    s = all_tools()["sim.run"].handler(ctx, test="fifo_smoke", seed=7)
    cmd = all_tools()["repro"].handler(ctx, job=s["job"])["repro"]
    assert "iverilog" in cmd and "seed=7" in cmd


def test_tool_catalog_shapes():
    tools = all_tools()
    assert "report.done" in tools
    assert tools["report.done"].permission == "submit"
    assert tools["cov.exclude"].permission == "approve"  # human-gated
    # every tool has a schema and a cost/permission
    for t in tools.values():
        assert t.schema and t.cost and t.permission
