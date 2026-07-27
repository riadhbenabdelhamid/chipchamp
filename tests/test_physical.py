"""RTL-to-GDSII (physical vertical): LibreLane metrics normalizer, signoff
verdict, physical gates — validated against a REAL metrics.json from the counter
run. The live flow test is opt-in (CHIPCHAMP_LIVE_PNR=1) since it takes minutes."""
from __future__ import annotations

import json
import os

import pytest

from conftest import REPO
from chipchamp.adapters import AdapterRegistry
from chipchamp.adapters.librelane import (LibreLaneAdapter, normalize_metrics,
                                         signoff_verdict)
from chipchamp.policy.gates import Evidence, evaluate

FIXTURE = REPO / "tests" / "fixtures" / "librelane_counter_metrics.json"


def _metrics():
    return json.load(open(FIXTURE))


def test_normalizer_extracts_signoff_facts():
    n = normalize_metrics(_metrics())
    # the real counter run: clean everywhere, positive slack
    assert n["setup_ws_ns"] > 0 and n["hold_ws_ns"] > 0
    assert n["drc_violations"] == 0 and n["lvs_errors"] == 0
    assert n["antenna_violations"] == 0
    assert n["cell_area_um2"] > 0 and 0 < n["utilization"] <= 1


def test_setup_slack_is_worst_across_corners():
    m = _metrics()
    n = normalize_metrics(m)
    corner_vals = [v for k, v in m.items() if k.startswith("timing__setup__ws__corner:")]
    assert n["setup_ws_ns"] == min(corner_vals)  # worst corner, not nominal


def test_verdict_clean_and_fail():
    clean, fails = signoff_verdict(normalize_metrics(_metrics()))
    assert clean == "signoff-clean" and fails == []
    bad = normalize_metrics(_metrics())
    bad.update(setup_ws_ns=-0.1, drc_violations=5, lvs_errors=2, antenna_violations=3)
    verdict, fails = signoff_verdict(bad)
    assert verdict == "signoff-fail"
    assert len(fails) == 4  # setup, DRC, LVS, antenna


def test_physical_gates_pass_on_clean_run():
    class J:
        id = "J-PNR"; kind = "pnr"; status = "passed"; start_ts = 1; summary = ""
        result = {"metrics": normalize_metrics(_metrics())}
    rep = evaluate("physical", Evidence(jobs=[J()]))
    assert rep.all_passed
    assert {g.name for g in rep.gates} == {"drc_clean", "lvs_clean", "timing_met",
                                           "antenna_clean"}


def test_physical_gates_fail_on_violations():
    n = normalize_metrics(_metrics())
    n["drc_violations"] = 3
    n["setup_ws_ns"] = -0.2

    class J:
        id = "J-PNR"; kind = "pnr"; status = "failed"; start_ts = 1; summary = ""
        result = {"metrics": n}
    rep = evaluate("physical", Evidence(jobs=[J()]))
    assert not rep.all_passed
    assert "drc_clean" in rep.blocking and "timing_met" in rep.blocking
    assert "lvs_clean" not in rep.blocking  # LVS still clean


def test_physical_gate_missing_without_pnr_run():
    rep = evaluate("physical", Evidence(jobs=[]))
    assert not rep.all_passed
    assert all(g.status == "missing" for g in rep.gates)


def test_sdc_change_classifies_physical():
    from chipchamp.policy import Diff, FileChange
    d = Diff(files=[FileChange("constraints/counter.sdc", "modified")])
    from chipchamp.policy.classify import classify
    assert classify(d) == "physical"


def test_adapter_registered_and_config_generation(tmp_path):
    reg = AdapterRegistry()
    a = reg.get("librelane")
    assert a.name == "librelane" and "pnr" in a.manifest().roles
    plan = a.pnr([str(REPO / "examples/soc/rtl/counter.sv")], "counter",
                 str(tmp_path), clock_period=10)
    cfg = json.load(open(tmp_path / "config.counter.json"))
    assert cfg["DESIGN_NAME"] == "counter" and cfg["CLOCK_PERIOD"] == 10
    assert cfg["PDK"] == "sky130A"
    assert plan.kind == "pnr" and plan.artifacts["gds"].endswith("counter.gds")


@pytest.mark.skipif(os.environ.get("CHIPCHAMP_LIVE_PNR") != "1",
                    reason="set CHIPCHAMP_LIVE_PNR=1 to run the real RTL-to-GDSII flow (minutes)")
def test_live_rtl_to_gdsii(tmp_path):
    a = LibreLaneAdapter()
    if not a.available():
        pytest.skip("librelane not available")
    import subprocess
    import time
    from chipchamp.adapters.base import StepResult
    plan = a.pnr([str(REPO / "examples/soc/rtl/counter.sv")], "counter",
                 str(tmp_path), clock_period=10)
    results = []
    for st in plan.steps:
        t0 = time.time()
        p = subprocess.run(st.argv, cwd=st.cwd, capture_output=True, text=True, timeout=3600)
        results.append(StepResult(argv=st.argv, rc=p.returncode, stdout=p.stdout,
                                  stderr=p.stderr, duration_s=time.time() - t0))
    res = a.parse(plan, results)
    assert res.status == "signoff-clean", res.summary
    assert os.path.exists(res.artifacts["gds"])
