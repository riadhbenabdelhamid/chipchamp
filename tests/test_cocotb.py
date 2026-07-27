"""cocotb runner (SPEC §8.4, P4): live run of a Python coroutine testbench over
Icarus, plus results.xml parsing (fixture)."""
from __future__ import annotations

import shutil

import pytest

from chipchamp.adapters import AdapterRegistry
from chipchamp.adapters.cocotb_adapter import CocotbAdapter, _cocotb_python
from chipchamp.tools import all_tools

requires_cocotb = pytest.mark.skipif(
    _cocotb_python() is None, reason="cocotb interpreter (w/ VPI) not available")


def test_results_xml_parsing(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text("""<?xml version="1.0"?>
<testsuites><testsuite name="cocotb">
  <testcase name="ok" classname="t"/>
  <testcase name="bad" classname="t">
    <failure error_type="AssertionError" error_msg="count=3 expected 4"/>
  </testcase>
</testsuite></testsuites>""")
    from chipchamp.adapters.base import Plan, StepResult
    plan = Plan(kind="sim", adapter="cocotb", workdir=str(tmp_path),
                artifacts={"results": str(xml)}, steps=[])
    res = CocotbAdapter().parse(plan, [StepResult(argv=[], rc=0, stdout="", stderr="")])
    assert res.metrics == {"passed": 1, "failed": 1, "seed": None}
    assert res.status == "fail"
    assert any("count=3 expected 4" in d.message for d in res.diagnostics)


def test_missing_results_is_error(tmp_path):
    from chipchamp.adapters.base import Plan, StepResult
    plan = Plan(kind="sim", adapter="cocotb", workdir=str(tmp_path),
                artifacts={"results": str(tmp_path / "nope.xml")}, steps=[])
    res = CocotbAdapter().parse(plan, [StepResult(argv=[], rc=1, stdout="", stderr="")])
    assert res.status == "error" and not res.ok


@requires_cocotb
def test_cocotb_adapter_available_and_versioned():
    a = AdapterRegistry().get("cocotb")
    assert a.available()
    assert "cocotb" in a.version()


@requires_cocotb
def test_cocotb_testbench_runs_live(ctx):
    r = all_tools()["sim.run"].handler(ctx, test="counter_cocotb", seed=1)
    assert r.get("runner") == "cocotb"
    assert r["sim_status"] == "pass", r
