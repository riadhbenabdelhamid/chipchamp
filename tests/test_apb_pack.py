"""APB protocol pack (SPEC §15.3): live formal proof of the generated regblock,
plus a non-vacuity check — a protocol-violating mutant must FAIL the proof."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from conftest import EXAMPLE, REPO

from chipchamp.adapters import AdapterRegistry, StepResult
from chipchamp.packs import list_packs, pack_file

requires_sby = pytest.mark.skipif(shutil.which("sby") is None,
                                  reason="SymbiYosys not installed")

CHECKER = str(REPO / "packs" / "apb" / "apb_checker.sv")
HARNESS = str(REPO / "packs" / "apb" / "formal" / "apb_regblock_formal.sv")
REGBLOCK = str(EXAMPLE / "rtl" / "gen" / "timer_reg_top.sv")


def _run_formal(files, tmp_path, depth=10):
    sby = AdapterRegistry().get("symbiyosys")
    plan = sby.formal(files, "apb_regblock_formal", str(tmp_path),
                      mode="bmc", depth=depth)
    results = []
    for st in plan.steps:
        p = subprocess.run(st.argv, cwd=st.cwd, capture_output=True, text=True,
                           timeout=600)
        results.append(StepResult(argv=st.argv, rc=p.returncode,
                                  stdout=p.stdout, stderr=p.stderr))
    return sby.parse(plan, results)


def test_pack_discovery():
    packs = {p["name"]: p for p in list_packs()}
    assert "apb" in packs
    assert "apb_checker.sv" in packs["apb"]["files"]
    assert pack_file("apb", "apb_checker.sv") is not None


@requires_sby
def test_apb_pack_proves_generated_regblock(tmp_path):
    res = _run_formal([CHECKER, REGBLOCK, HARNESS], tmp_path)
    assert res.status == "pass", res.summary


@requires_sby
def test_protocol_violation_is_caught(tmp_path):
    """Non-vacuity: a slave that raises pslverr outside the access phase must
    fail the S2 assertion."""
    bad = tmp_path / "timer_reg_top.sv"
    src = open(REGBLOCK).read()
    assert "assign pslverr = psel & penable & ~sel_any;" in src
    bad.write_text(src.replace(
        "assign pslverr = psel & penable & ~sel_any;",
        "assign pslverr = ~sel_any;"))  # BUG: pslverr outside access
    res = _run_formal([CHECKER, str(bad), HARNESS], tmp_path)
    assert res.status == "fail", "mutant slave should violate the APB pack"
