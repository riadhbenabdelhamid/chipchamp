"""pd.area (liberty-mapped per-module area) + pd.ppa snapshot/delta."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.physical.area import (find_liberty, parse_stat_json, ppa_delta,
                                    ppa_snapshot, rollup)
from chipchamp.tools import all_tools

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

requires_yosys_liberty = pytest.mark.skipif(
    not shutil.which("yosys") or find_liberty() is None,
    reason="needs yosys and a sky130 liberty (ciel/volare or CHIPCHAMP_LIBERTY)")


def test_parse_stat_json_real_librelane_report():
    # the REAL stat.json from the kept LibreLane counter run
    src = os.path.join(FIX, "..", "..", "examples", "soc", ".chipchamp", "pd",
                       "counter", "runs", "chipchamp", "06-yosys-synthesis",
                       "reports", "stat.json")
    if not os.path.exists(src):
        pytest.skip("kept run not present")
    per = parse_stat_json(src)
    assert "counter" in per
    assert per["counter"]["area_um2"] == pytest.approx(415.4, abs=0.01)
    assert per["counter"]["sequential_area_um2"] == pytest.approx(210.2, abs=0.01)
    assert per["counter"]["cells_by_type"]["sky130_fd_sc_hd__dfrtp_2"] == 8


def test_rollup_sums_submodules():
    per = {
        "top": {"area_um2": 10.0, "sequential_area_um2": 0, "cells": 3,
                "cells_by_type": {"leaf": 2, "sky130_fd_sc_hd__inv_2": 1}},
        "leaf": {"area_um2": 5.0, "sequential_area_um2": 0, "cells": 1,
                 "cells_by_type": {"sky130_fd_sc_hd__nand2_2": 1}},
    }
    r = rollup(per)
    assert r["leaf"]["total_area_um2"] == 5.0
    assert r["top"]["total_area_um2"] == 20.0  # 10 + 2×5


def test_ppa_snapshot_and_delta_directions():
    base = ppa_snapshot({"setup_ws_ns": 1.6, "cell_area_um2": 700.0,
                         "power_w": 1e-4, "drc_violations": 0})
    cur = ppa_snapshot({"setup_ws_ns": 0.9, "cell_area_um2": 650.0,
                        "power_w": 2e-4, "drc_violations": 0})
    d = ppa_delta(cur, base)
    assert d["setup_ws_ns"]["delta"] == pytest.approx(-0.7)
    assert d["setup_ws_ns"]["better"] is False   # lost slack
    assert d["cell_area_um2"]["better"] is True  # shrank
    assert d["power_w"]["better"] is False       # more power
    assert d["drc_violations"]["delta"] == 0


@requires_yosys_liberty
def test_pd_area_live_per_module(ctx):
    """Whole soc_top mapped to sky130 in seconds; every module priced."""
    r = all_tools()["pd.area"].handler(ctx, top="soc_top")
    assert "error" not in r, r
    assert r["mapped"] and r["liberty"].startswith("sky130_fd_sc_hd")
    # parameterized modules come back demangled ("counter #(WIDTH=8)")
    mods = {row["module"].split(" #(")[0]: row for row in r["by_module"]}
    assert {"soc_top", "counter", "sync_fifo"} <= set(mods)
    assert mods["counter"]["design_area_um2"] > 100  # 8+ flops + logic
    # top's rollup covers its parts
    assert r["total_area_um2"] >= mods["counter"]["design_area_um2"]
    assert r["by_module"][0]["module"] == "soc_top"  # sorted desc by design area


@requires_yosys_liberty
def test_pd_ppa_baseline_delta_live(ctx):
    """Snapshot the kept pnr run as baseline, then delta against itself."""
    import glob as _glob
    if not _glob.glob(os.path.join(str(ctx.ws.dot), "runs", "*", "record.json")):
        pytest.skip("no job records")
    base = all_tools()["pd.ppa"].handler(ctx, module="counter", save_baseline=True)
    if "error" in base:
        pytest.skip(f"no pnr job on disk: {base['error']}")
    assert base["baseline_saved"]
    again = all_tools()["pd.ppa"].handler(ctx, module="counter")
    assert "delta" in again
    # identical run → every delta zero, nothing regressed
    assert not again["regressions"]
    assert all(v["delta"] == 0 for v in again["delta"].values()
               if isinstance(v, dict) and "delta" in v)
