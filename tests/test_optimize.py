"""P11 optimize playbook: evidence-driven PPA brief with a ranked worklist.
Missing evidence becomes measurement work items, never silence."""
from __future__ import annotations

import shutil

import pytest

from chipchamp.agent.playbooks import run_optimize
from chipchamp.physical.area import find_liberty
from chipchamp.tools import all_tools

requires_stack = pytest.mark.skipif(
    not shutil.which("yosys") or not shutil.which("verilator")
    or find_liberty() is None,
    reason="needs yosys + verilator + sky130 liberty")


@requires_stack
def test_optimize_brief_with_coverage(ctx):
    cov = all_tools()["cov.run"].handler(ctx, test="fifo_smoke", set="default")
    assert "error" not in cov, cov
    r = run_optimize(ctx, top="soc_top")
    # whole-design hotspots: the FIFO's memory line dominates
    hot = dict(r["area_hotspots"])
    assert any(k.startswith("sync_fifo.sv") for k in hot), hot.keys()
    top_line, top_e = r["area_hotspots"][0]
    assert top_e["area_um2"] > 1000
    assert r["sweep"]["pareto"]
    # with toggle coverage present, dead-width findings become work items
    kinds = {w["kind"] for w in r["worklist"]}
    assert "dead-width" in kinds, r["worklist"]
    dw = next(w for w in r["worklist"] if w["kind"] == "dead-width")
    assert "lec.run" in dw["loop"]  # every item names the proof step
    assert "note" in r and "lec.run" in r["note"]


@requires_stack
def test_optimize_brief_missing_evidence_becomes_work(ctx):
    """Fresh context (no coverage, soc_top has no routed run): the brief must
    ask for the measurements instead of going quiet."""
    r = run_optimize(ctx, top="soc_top")
    kinds = [w["kind"] for w in r["worklist"]]
    assert kinds.count("measurement") >= 2  # toggle coverage + power
    evidence = " ".join(w["evidence"] for w in r["worklist"])
    assert "toggle coverage" in evidence and "power" in evidence
