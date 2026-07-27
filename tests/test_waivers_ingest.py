"""Waivers-as-data (§12.6), FST bridge (FR-WAVE-01), FuseSoC/Bender ingestion
(§12.2), env-modules wrapping (FR-PROJ-03)."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

from chipchamp.adapters.base import NormalizedDiagnostic
from chipchamp.policy.waivers import Waiver, apply_waivers, draft_waiver, load_waivers


def _diag(code="UNUSEDSIGNAL", file="rtl/old_uart.sv", sev="error"):
    return NormalizedDiagnostic(tool="t", severity=sev, message="m",
                                code=code, file=file, line=1)


def test_only_active_approved_waivers_suppress():
    diags = [_diag(), _diag(code="WIDTH", file="rtl/dma.sv")]
    waivers = [
        Waiver(rule="UNUSEDSIGNAL", scope="rtl/old_uart.sv", status="active",
               approver="sam", justification="legacy block"),
        Waiver(rule="WIDTH", scope="rtl/dma.sv", status="proposed",  # NOT active
               justification="drafted by agent"),
    ]
    stats = apply_waivers(diags, waivers)
    assert diags[0].waiver is not None          # active + approved -> waived
    assert diags[1].waiver is None              # proposed -> never suppresses
    assert stats["waived_diagnostics"] == 1 and stats["active_waivers"] == 1


def test_expired_and_unapproved_waivers_inactive():
    assert not Waiver(rule="X", scope="*", status="active", approver=None).is_active()
    assert not Waiver(rule="X", scope="*", status="active", approver="sam",
                      expires="2020-01-01").is_active()
    assert Waiver(rule="X", scope="*", status="active", approver="sam",
                  expires="2099-01-01").is_active()


def test_waiver_file_loading_and_gate_math(tmp_path):
    wdir = tmp_path / "waivers"
    wdir.mkdir()
    (wdir / "lint.yaml").write_text(yaml.safe_dump([
        {"rule": "UNUSED*", "scope": "rtl/*.sv", "status": "active",
         "approver": "sam", "justification": "cleanup scheduled"}]))
    waivers = load_waivers(str(tmp_path))
    assert len(waivers) == 1
    d = _diag(code="UNUSEDSIGNAL", file="rtl/x.sv")
    apply_waivers([d], waivers)
    assert d.waiver == "UNUSED*@rtl/*.sv"
    # gate math: an error with a waiver mark doesn't block (see gates.py 'lint')
    from chipchamp.policy.gates import Evidence, evaluate

    class J:
        id = "J-1"; kind = "lint"; status = "passed"; summary = ""
        result = {"diagnostics": [d.__dict__]}
    rep = evaluate("rtl_nfc", Evidence(jobs=[J()]))
    lint_gate = next(g for g in rep.gates if g.name == "lint")
    assert lint_gate.ok and "waived" in lint_gate.detail


def test_draft_waiver_goes_to_staging_not_waivers_dir(tmp_path):
    p = draft_waiver(str(tmp_path / ".chipchamp"), rule="WIDTH",
                     scope="rtl/legacy.sv", justification="legacy, tracked in HYDRA-99")
    assert ".chipchamp" in p and "proposed_waivers.yaml" in p
    entries = yaml.safe_load(open(p))
    assert entries[0]["status"] == "proposed" and entries[0]["approver"] is None


@pytest.mark.skipif(shutil.which("fst2vcd") is None or shutil.which("vcd2fst") is None,
                    reason="gtkwave fst tools not on PATH")
def test_fst_bridge_roundtrip(tmp_path):
    """FR-WAVE-01: write VCD -> convert to FST -> WaveStore.open(.fst) answers
    the same queries."""
    from chipchamp.waves import WaveStore
    vcd = tmp_path / "w.vcd"
    vcd.write_text("""$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 4 # cnt $end
$upscope $end
$enddefinitions $end
#0
0!
b0 #
#10
1!
b101 #
#20
0!
b1111 #
""")
    fst = tmp_path / "w.fst"
    subprocess.run(["vcd2fst", str(vcd), str(fst)], check=True, timeout=60)
    ws = WaveStore.open(str(fst))
    assert ws.value("tb.cnt", 15) == "101"
    assert ws.when("cnt == 4'hF")["time"] == 20


def test_fusesoc_core_ingestion(tmp_path):
    from chipchamp.filelist import parse_fusesoc_core
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule")
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "defs.svh").write_text("`define X 1")
    core = tmp_path / "ip.core"
    core.write_text("""CAPI=2:
name: acme:ip:demo:1.0
filesets:
  rtl:
    files:
      - rtl/a.sv
      - inc/defs.svh: {is_include_file: true}
targets:
  default:
    filesets: [rtl]
""")
    fs = parse_fusesoc_core(str(core))
    assert any(s.endswith("rtl/a.sv") for s in fs.sources)
    assert any(d.endswith("inc") for d in fs.incdirs)


def test_bender_manifest_ingestion(tmp_path):
    from chipchamp.filelist import parse_bender_manifest
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.sv").write_text("module top; endmodule")
    m = tmp_path / "Bender.yml"
    m.write_text("""package:
  name: demo
sources:
  - src/top.sv
  - include_dirs: [src]
    defines: {SYNTHESIS: ~}
    files: [src/top.sv]
""")
    fs = parse_bender_manifest(str(m))
    assert any(s.endswith("src/top.sv") for s in fs.sources)
    assert any(d.endswith("src") for d in fs.incdirs)
    assert fs.defines.get("SYNTHESIS") == "1"


def test_env_module_wrapping(monkeypatch):
    from chipchamp.jobs.runner import wrap_with_modules
    argv = ["vcs", "-full64", "tb.sv"]
    # without a modules system: unwrapped
    monkeypatch.delenv("MODULESHOME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *_: None)
    assert wrap_with_modules(argv, ["synopsys/vcs/2024.09"]) == argv
    # with MODULESHOME: wrapped in a login shell that loads first
    monkeypatch.setenv("MODULESHOME", "/usr/share/modules")
    wrapped = wrap_with_modules(argv, ["synopsys/vcs/2024.09"])
    assert wrapped[:2] == ["bash", "-lc"]
    assert "module load synopsys/vcs/2024.09" in wrapped[2]
    assert "exec vcs -full64 tb.sv" in wrapped[2]
