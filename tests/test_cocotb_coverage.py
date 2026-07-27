"""LIVE functional coverage on open tools: cocotb-coverage covergroups over an
Icarus sim → XML export → cov.load → holes + baseline gate. The whole
functional-coverage loop with zero licenses."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from chipchamp.adapters.cocotb_adapter import _cocotb_python
from chipchamp.tools import all_tools


def _cocotb_python_has_coverage() -> bool:
    """The cocotb interpreter (tabbypy3/…) must have cocotb_coverage — it is
    pure-python: `tabbypy3 -m pip install cocotb-coverage`."""
    py = _cocotb_python()
    if py is None:
        return False
    try:
        return subprocess.run([py, "-c", "import cocotb_coverage"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_cocotb_cov = pytest.mark.skipif(
    not shutil.which("iverilog") or not _cocotb_python_has_coverage(),
    reason="needs a cocotb interpreter with cocotb-coverage and iverilog on PATH")


@requires_cocotb_cov
def test_functional_coverage_live_loop(ctx):
    xml = os.path.join(ctx.ws.root, "test_counter_cov_cocotb.cov.xml")
    if os.path.exists(xml):
        os.unlink(xml)

    r = all_tools()["sim.run"].handler(ctx, test="counter_cov_cocotb", seed=1)
    assert r.get("runner") == "cocotb", r
    assert r["sim_status"] == "pass", r
    assert r.get("coverage_xml"), "sim.run did not surface the coverage artifact"
    assert os.path.exists(xml), "coverage XML was not exported"

    # ingest through the sniffing loader
    r2 = all_tools()["cov.load"].handler(ctx, path=r["coverage_xml"],
                                         set="func", test="counter_cov_cocotb")
    assert r2["format"] == "cocotb-xml"
    assert r2["bins_loaded"] >= 10  # 4+4+2 points + cross bins
    assert 0 < r2["summary"]["total"] < 100  # deliberate hole -> not 100%

    # the planted hole surfaces: (en=0, clr=1) is never driven
    holes = all_tools()["cov.holes"].handler(ctx, set="func", kind="functional")
    ids = {h["bin"] for h in holes["holes"]}
    assert any("ctrl" in i and "(0, 1)" in i for i in ids), ids
    # rollover WAS exercised (300 cycles wraps the 8-bit counter)
    assert not any(i.endswith("rollover::True") for i in ids), ids

    # attribution ties hits back to the test
    attr = all_tools()["cov.attribution"].handler(
        ctx, set="func", test="counter_cov_cocotb")
    assert len(attr["bins"]) == r2["summary"]["bins"] - len(ids)
