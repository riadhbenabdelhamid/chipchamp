"""The RapidWright adapter and the three checkpoint tools, without
RapidWright installed: plans, report parsing, refusal paths, catalog wiring.
A live test runs only when the package and a checkpoint are present."""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from chipchamp.adapters.base import StepResult
from chipchamp.adapters.rapidwright import ACTIONS, INSTALL_HINT, RapidWrightAdapter
from chipchamp.adapters.registry import AdapterRegistry, _BUILTIN
from chipchamp.tools import all_tools

STATS = {"checkpoint": "x.dcp", "part": "xc7a35tcpg236-1", "device": "xc7a35t",
         "series": "Series7", "top": "counter",
         "cells": {"total": 42, "placed": 42, "unplaced": 0,
                   "by_family": {"logic": 38, "io": 3, "clock": 1},
                   "by_type": {"LUT2": 10, "FDRE": 20}},
         "sites_used": 20, "tiles_used": 9, "clock_regions": {"X0Y0": 42},
         "device_grid": {"columns": 100, "rows": 150},
         "nets": {"total": 60, "routed": 57, "unrouted": 0, "static": 3, "clock": 1,
                  "unrouted_names": []},
         "fully_routed": True, "seconds": 7.5}


def _step(rc=0, err=""):
    return StepResult(argv=["x"], rc=rc, stdout="", stderr=err)


# ---- adapter ----------------------------------------------------------------

def test_registered_with_the_dcp_role():
    assert "rapidwright" in _BUILTIN
    ad = AdapterRegistry().get("rapidwright")
    assert ad.manifest().roles == ["dcp"] and "dcp" in ad.manifest().standards
    assert AdapterRegistry().for_role("dcp").name == "rapidwright"


def test_unavailable_without_the_package(monkeypatch):
    ad = RapidWrightAdapter()
    monkeypatch.setattr(ad, "_package_importable", lambda: False)
    assert ad.available() is False and ad.version() == "not-installed"


def test_plans_run_the_worker_in_a_separate_interpreter(tmp_path):
    ad = RapidWrightAdapter()
    dcp = str(tmp_path / "post_route.dcp")
    for action, script in ACTIONS.items():
        plan = ad.plan(action, dcp, str(tmp_path / action),
                       highlights=[{"match": "u_fifo"}] if action == "placement" else None)
        argv = plan.steps[0].argv
        assert argv[:3] == [sys.executable, "-m", f"chipchamp.adapters.rw_scripts.{script}"]
        assert argv[3] == dcp and plan.artifacts["report"].endswith(f"rw_{action}.json")
        assert plan.kind == "rapidwright" and plan.meta["action"] == action
    hl = tmp_path / "placement" / "placement_highlights.json"
    assert json.load(open(hl)) == [{"match": "u_fifo"}]
    with pytest.raises(ValueError):
        ad.plan("nope", dcp, str(tmp_path))


def test_parse_reads_the_stats_report(tmp_path):
    ad = RapidWrightAdapter()
    plan = ad.plan("stats", "x.dcp", str(tmp_path))
    json.dump(STATS, open(plan.artifacts["report"], "w"))
    res = ad.parse(plan, [_step()])
    assert res.ok and res.status == "passed" and res.kind == "rapidwright"
    assert res.metrics["cells"] == 42 and res.metrics["fully_routed"] is True
    assert res.metrics["clock_regions"] == 1 and "0 unrouted" in res.summary


def test_parse_reports_a_worker_error_as_a_job_error(tmp_path):
    ad = RapidWrightAdapter()
    plan = ad.plan("stats", "x.dcp", str(tmp_path))
    json.dump({"error": "ImportError: the `rapidwright` package is not importable"},
              open(plan.artifacts["report"], "w"))
    res = ad.parse(plan, [_step(rc=1)])
    assert not res.ok and res.status == "error" and "not importable" in res.summary
    assert res.diagnostics[0].tool == "rapidwright"
    plan2 = ad.plan("placement", "x.dcp", str(tmp_path / "p"))
    res = ad.parse(plan2, [_step(rc=1, err="java: not found")])     # no report at all
    assert res.status == "error" and "wrote no report" in res.summary and "java" in res.summary


def test_placement_and_interchange_summaries(tmp_path):
    ad = RapidWrightAdapter()
    plan = ad.plan("placement", "x.dcp", str(tmp_path / "p"))
    png = plan.artifacts["png"]
    open(png, "wb").write(b"\x89PNG")
    json.dump({"part": "p", "png": png, "placed_cells": 42, "tiles_used": 9,
               "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4}, "highlights": {"u_fifo": 5}},
              open(plan.artifacts["report"], "w"))
    res = ad.parse(plan, [_step()])
    assert res.ok and res.metrics["highlights"] == {"u_fifo": 5} and "png" in res.artifacts
    plan = ad.plan("interchange", "x.dcp", str(tmp_path / "i"))
    json.dump({"part": "p", "top": "counter", "bytes": {"logical_netlist": 10, "physical_netlist": 20}},
              open(plan.artifacts["report"], "w"))
    res = ad.parse(plan, [_step()])
    assert res.ok and res.metrics["top"] == "counter" and "counter" in res.summary


# ---- tools ------------------------------------------------------------------

def test_three_tools_are_in_the_catalog():
    t = all_tools()
    for name in ("fpga.stats", "fpga.placement", "fpga.interchange"):
        assert t[name].permission == "submit" and t[name].cost == "metered" and t[name].group == "fpga"


def test_tools_refuse_clearly_without_rapidwright(ctx, monkeypatch):
    monkeypatch.setattr(RapidWrightAdapter, "available", lambda self: False)
    out = all_tools()["fpga.stats"].handler(ctx)
    assert "not available" in out["error"] and out["hint"] == INSTALL_HINT


def test_tools_need_a_checkpoint(ctx, monkeypatch):
    monkeypatch.setattr(RapidWrightAdapter, "available", lambda self: True)
    out = all_tools()["fpga.stats"].handler(ctx)
    assert "no fpga run recorded" in out["error"] and "dcp=" in out["error"]
    assert "checkpoint not found" in all_tools()["fpga.placement"].handler(ctx, dcp="work/none.dcp")["error"]
    assert "workspace-relative" in all_tools()["fpga.interchange"].handler(ctx, dcp="/tmp/x.dcp")["error"]


def test_tool_runs_the_adapter_as_a_job(ctx, monkeypatch):
    """A worker that exits with a report (fake interpreter) is a normal job."""
    monkeypatch.setattr(RapidWrightAdapter, "available", lambda self: True)
    wsroot = str(ctx.ws.root)
    fake = os.path.join(wsroot, "fakepy.py")
    with open(fake, "w") as fh:        # "python -m module dcp report ..." -> writes the report
        fh.write("import json,sys\njson.dump(json.loads(%r), open(sys.argv[4], 'w'))\n"
                 % json.dumps(STATS))
    wrapper = os.path.join(wsroot, "fakepython")
    with open(wrapper, "w") as fh:
        fh.write(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
    os.chmod(wrapper, 0o755)
    monkeypatch.setenv("CHIPCHAMP_RAPIDWRIGHT_PYTHON", wrapper)
    dcp = os.path.join(wsroot, "work", "post_route.dcp")
    os.makedirs(os.path.dirname(dcp), exist_ok=True)
    open(dcp, "wb").write(b"dcp")
    out = all_tools()["fpga.stats"].handler(ctx, dcp="work/post_route.dcp")
    assert out["status"] == "passed", out
    assert out["report"]["cells"]["total"] == 42 and out["job"].startswith("J-")
    assert ctx.runner.get(out["job"]).kind == "rapidwright"


@pytest.mark.skipif(not (shutil.which("java") and RapidWrightAdapter()._package_importable()),
                    reason="needs java + the rapidwright package")
def test_live_worker_reports_an_unreadable_checkpoint(tmp_path):
    ad = RapidWrightAdapter()
    bad = tmp_path / "bad.dcp"
    bad.write_bytes(b"not a checkpoint")
    plan = ad.plan("stats", str(bad), str(tmp_path / "out"))
    import subprocess
    subprocess.run(plan.steps[0].argv, cwd=str(tmp_path), timeout=600)
    res = ad.parse(plan, [_step(rc=1)])
    assert res.status == "error" and res.summary
