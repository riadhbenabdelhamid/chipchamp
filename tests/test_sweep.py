"""pd.sweep: Pareto dominance logic + the live synth-mode sweep (yosys ABC
mapping-script knob + real OpenSTA delays per config)."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.physical.area import find_liberty
from chipchamp.physical.sweep import SYNTH_CONFIGS, pareto
from chipchamp.tools import all_tools


def test_pareto_dominance():
    pts = [{"config": "a", "area": 10.0, "delay": 5.0},
           {"config": "b", "area": 12.0, "delay": 4.0},
           {"config": "c", "area": 12.5, "delay": 4.5},   # dominated by b
           {"config": "d", "area": 10.0, "delay": 5.0}]   # tie with a: both live
    pareto(pts, {"area": "min", "delay": "min"})
    by = {p["config"]: p["pareto"] for p in pts}
    assert by == {"a": True, "b": True, "c": False, "d": True}


def test_pareto_none_safe():
    pts = [{"config": "a", "area": 10.0, "delay": None},
           {"config": "b", "area": 11.0, "delay": 3.0}]
    pareto(pts, {"area": "min", "delay": "min"})
    # missing an axis can't dominate on it; both survive
    assert all(p["pareto"] for p in pts)


def test_pareto_max_axis():
    pts = [{"config": "a", "area": 10.0, "slack": 1.0},
           {"config": "b", "area": 10.0, "slack": 2.0}]
    pareto(pts, {"area": "min", "slack": "max"})
    assert not pts[0]["pareto"] and pts[1]["pareto"]


requires_sweep = pytest.mark.skipif(
    not shutil.which("yosys") or find_liberty() is None,
    reason="needs yosys + sky130 liberty")


@requires_sweep
def test_pd_sweep_synth_live(ctx):
    r = all_tools()["pd.sweep"].handler(ctx, top="sync_fifo",
                                        clock_period_ns=10.0)
    assert "error" not in r, r
    assert {row["config"] for row in r["table"]} == set(SYNTH_CONFIGS)
    for row in r["table"]:
        assert row["area_um2"] and row["area_um2"] > 1000  # fifo is real
        assert row["job"].startswith("J-")
    assert r["pareto"], "at least one config must be non-dominated"
    # every pareto member must not be dominated on the reported axes
    rows = {row["config"]: row for row in r["table"]}
    for name in r["pareto"]:
        assert rows[name]["pareto"]
    # with the devshell present we get REAL delays and can check dominance
    if any(row["delay_ns"] for row in r["table"]):
        live = [row for row in r["table"] if row["delay_ns"]]
        best_area = min(live, key=lambda x: x["area_um2"])
        assert best_area["config"] in r["pareto"]
