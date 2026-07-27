"""eFPGA-FABulous vertical (FABulous): adapter flow/command construction, result parsing,
and gates. The live flow (fabric gen + bitstream) is opt-in (CHIPCHAMP_LIVE_EFPGA=1,
needs FABulous + nextpnr) since it takes minutes."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.adapters import AdapterRegistry
from chipchamp.adapters.base import Plan, StepResult
from chipchamp.adapters.fabulous import FabulousAdapter
from chipchamp.policy.gates import Evidence, evaluate


def test_adapter_registered_with_efpga_role():
    reg = AdapterRegistry()
    a = reg.get("fabulous")
    assert a.name == "fabulous" and "efpga-fabulous" in a.manifest().roles
    assert reg.for_role("efpga-fabulous").name == "fabulous"


def test_flow_command_construction():
    a = FabulousAdapter()
    p = a.bitstream("/proj", "user_design/foo.v")
    step = p.steps[0]
    assert step.argv[:4] == ["FABulous", "-p", "/proj", "run"]
    assert "run_FABulous_bitstream user_design/foo.v" in step.argv[4]
    assert p.artifacts["bitstream"].endswith("user_design/foo.bin")
    assert p.artifacts["fasm"].endswith("user_design/foo.fasm")
    # with_fabric prepends fabric generation
    p2 = a.bitstream("/proj", "user_design/foo.v", with_fabric=True)
    assert p2.steps[0].argv[4].startswith("run_FABulous_fabric; ")
    # fabric-only + harden
    assert a.gen_fabric("/proj").meta["step"] == "fabric"
    assert "run_FABulous_eFPGA_macro" in a.harden("/proj").steps[0].argv[4]


def test_parse_success_from_real_shaped_artifacts(tmp_path):
    proj = tmp_path
    (proj / "user_design").mkdir()
    (proj / "Fabric").mkdir()
    (proj / "Tile").mkdir()
    bit = proj / "user_design" / "foo.bin"
    bit.write_bytes(b"\x00\x01" * 6000)                        # 12000-byte bitstream
    (proj / "user_design" / "foo_npnr_log.txt").write_text(
        "Info: Placement ...\nInfo: Program finished normally.\n")
    (proj / "Fabric" / "eFPGA.v").write_text("module eFPGA-FABulous(); endmodule")
    (proj / "Tile" / "lut.v").write_text("module lut(); endmodule")

    a = FabulousAdapter()
    plan = a.bitstream(str(proj), "user_design/foo.v")
    step = StepResult(argv=[], rc=0, stdout="Commands ... executed successfully\n",
                      stderr="")
    res = a.parse(plan, [step])
    assert res.ok and res.status == "ok"
    assert res.metrics["bitstream_bytes"] == 12000
    assert res.metrics["routed"] is True
    assert res.metrics["fabric_generated"] is True
    assert res.metrics["fabric_files"] >= 2
    assert res.artifacts["bitstream"].endswith("foo.bin")


def test_parse_failure_when_no_bitstream(tmp_path):
    a = FabulousAdapter()
    plan = a.bitstream(str(tmp_path), "user_design/foo.v")  # no .bin exists
    res = a.parse(plan, [StepResult(argv=[], rc=1, stdout="ERROR: route failed",
                                    stderr="")])
    assert not res.ok and res.status == "error"
    assert res.metrics["bitstream_bytes"] == 0


def _efpga_job(bytes_=12000, routed=True, fabric=True):
    class J:
        id = "J-EFP"; kind = "efpga-fabulous"; status = "passed"; start_ts = 1; summary = ""
        result = {"metrics": {"bitstream_bytes": bytes_, "routed": routed,
                              "fabric_generated": fabric, "fabric_files": 55}}
    return J()


def test_efpga_gates_pass():
    rep = evaluate("efpga-fabulous", Evidence(jobs=[_efpga_job()]))
    assert rep.all_passed
    assert {g.name for g in rep.gates} == {"fabric_generated", "bitstream_generated"}


def test_efpga_gates_fail_when_unrouted_or_empty():
    rep = evaluate("efpga-fabulous", Evidence(jobs=[_efpga_job(bytes_=0, routed=False)]))
    assert not rep.all_passed and "bitstream_generated" in rep.blocking


def test_efpga_gate_missing_without_run():
    rep = evaluate("efpga-fabulous", Evidence(jobs=[]))
    assert all(g.status == "missing" for g in rep.gates)


def test_efpga_tools_registered():
    from chipchamp.tools import all_tools
    t = all_tools()
    for n in ("efpga-fabulous.fabric", "efpga-fabulous.bitstream", "efpga-fabulous.harden", "efpga-fabulous.info"):
        assert n in t and t[n].group == "efpga-fabulous"


@pytest.mark.skipif(os.environ.get("CHIPCHAMP_LIVE_EFPGA") != "1" or
                    shutil.which("FABulous") is None,
                    reason="set CHIPCHAMP_LIVE_EFPGA=1 with FABulous+nextpnr for the live flow")
def test_live_efpga_bitstream(tmp_path):
    import subprocess
    proj = str(tmp_path / "proj")
    subprocess.run(["FABulous", "create-project", proj], check=True, timeout=180)
    a = FabulousAdapter()
    plan = a.bitstream(proj, "user_design/sequential_16bit_en.v", with_fabric=True)
    st = plan.steps[0]
    p = subprocess.run(st.argv, cwd=st.cwd, capture_output=True, text=True, timeout=1800)
    res = a.parse(plan, [StepResult(argv=st.argv, rc=p.returncode, stdout=p.stdout,
                                    stderr=p.stderr)])
    assert res.ok, res.summary
    assert res.metrics["bitstream_bytes"] > 0 and res.metrics["routed"]
