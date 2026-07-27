"""Commercial adapter normalizers (fixture-validated, SPEC §8.4 M2) and farm
command wrapping (dry-run, SPEC §8.5)."""
from __future__ import annotations

from chipchamp.adapters import AdapterRegistry
from chipchamp.adapters.questa import parse_questa
from chipchamp.adapters.vcs import parse_vcs
from chipchamp.jobs.farm import FarmConfig, plan_submission, wrap_for_farm

QUESTA_LOG = """\
QuestaSim-64 vlog 2025.1 Compiler
** Warning: (vlog-2583) rtl/dma_wr.sv(88): [SVCHK] - Extra checking...
** Error: rtl/dma_wr.sv(214): (vlog-2892) Net type of 'grant' was not declared.
** Error (suppressible): tb/tb_dma.sv(12): (vlog-13233) Design unit "dma_pkg" is...
** Fatal: (vsim-3695) The FLI is unavailable.
"""

VCS_LOG = """\
Error-[ICPD] Illegal combination of ports
"rtl/dma_wr.sv", 214
  Port 'grant' driven by multiple always blocks.

Warning-[LINT-1] Signal unused
"rtl/dma_rd.sv", 45
  Signal 'spare' is never read.

Error-[SE] Syntax error
  Following verilog source has syntax error :
"""


def test_questa_normalizer_fixture():
    diags = parse_questa(QUESTA_LOG)
    errs = [d for d in diags if d.severity == "error"]
    warns = [d for d in diags if d.severity == "warning"]
    assert len(errs) == 3 and len(warns) == 1  # Fatal counts as error
    d = next(d for d in errs if d.code == "vlog-2892")
    assert d.file == "rtl/dma_wr.sv" and d.line == 214
    assert "grant" in d.message


def test_vcs_normalizer_fixture():
    diags = parse_vcs(VCS_LOG)
    errs = [d for d in diags if d.severity == "error"]
    assert {d.code for d in errs} == {"ICPD", "SE"}
    icpd = next(d for d in errs if d.code == "ICPD")
    assert icpd.file == "rtl/dma_wr.sv" and icpd.line == 214
    lint = next(d for d in diags if d.code == "LINT-1")
    assert lint.severity == "warning" and lint.line == 45


def test_commercial_adapters_registered_with_manifests():
    reg = AdapterRegistry()
    mans = {m["adapter"]: m for m in reg.all_manifests()}
    for name, role in (("questa", "sim"), ("vcs", "sim"), ("primetime", "sta")):
        assert name in mans
        assert role in mans[name]["roles"]
        # honest about validation status on machines without the tool
        assert any("pending" in g for g in mans[name]["known_gaps"])
    # license features declared (license-aware scheduling input)
    q = reg.get("questa")
    assert q.manifest().license_features == ["questa_sim"]
    assert q.cost == "licensed"


def test_questa_plan_declares_license_tokens(tmp_path):
    reg = AdapterRegistry()
    q = reg.get("questa")
    plan = q.sim(["a.sv"], "tb_top", str(tmp_path), seed=7)
    assert plan.license_features == ["questa_sim"]
    assert any("vlog" in s.argv[0] for s in plan.steps)
    assert any("vsim" in s.argv[0] for s in plan.steps)
    assert any("-sv_seed" in s.argv for s in plan.steps)


def test_lsf_wrapping():
    farm = FarmConfig(kind="lsf", queue="normal", resources="rusage[mem=16G]")
    argv = wrap_for_farm(["vvp", "sim.vvp"], farm, job_name="J-0007")
    assert argv[:5] == ["bsub", "-K", "-J", "J-0007", "-q"]
    assert "rusage[mem=16G]" in argv
    assert argv[-2:] == ["vvp", "sim.vvp"]


def test_slurm_wrapping():
    farm = FarmConfig(kind="slurm", queue="eda", resources="--mem=8G --cpus-per-task=4")
    argv = wrap_for_farm(["verilator", "--binary"], farm)
    assert argv[0] == "srun"
    assert "--partition=eda" in argv and "--mem=8G" in argv
    assert argv[-2:] == ["verilator", "--binary"]


def test_missing_scheduler_degrades_to_local_with_note():
    farm = FarmConfig(kind="lsf", queue="normal")
    argv, note = plan_submission(["echo", "hi"], farm)  # no bsub on this machine
    assert argv == ["echo", "hi"]
    assert "not on PATH" in note and "ran locally" in note


def test_dry_run_shows_submission_even_without_scheduler():
    farm = FarmConfig(kind="slurm", queue="eda")
    argv, note = plan_submission(["echo", "hi"], farm, dry_run=True)
    assert argv[0] == "srun" and note == "slurm"
