"""Behavioral regressions for the verification-integrity review."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chipchamp.adapters.base import Adapter, Plan, Step, ToolResult
from chipchamp.config import Workspace
from chipchamp.coverage.model import Bin, CoverageModel
from chipchamp.coverage.service import CoverageService
from chipchamp.jobs.cache import cache_key
from chipchamp.jobs.record import JobRecord
from chipchamp.jobs.runner import JobRunner
from chipchamp.policy import Diff, Evidence, FileChange, PolicyEngine, evaluate
from chipchamp.tools.context import ToolContext
from chipchamp.tools.fs_tools import fs_edit, fs_grep, fs_list, fs_read, fs_write
from chipchamp.tools.meta_tools import report_done


class PassingAdapter(Adapter):
    name = "review-pass"

    def version(self):
        return "1"

    def parse(self, plan, results):
        return ToolResult(ok=True, kind=plan.kind, adapter=self.name,
                          status="pass" if plan.kind == "sim" else "passed")


def context(tmp_path):
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl/m.sv").write_text("module m; endmodule\n")
    return ToolContext(Workspace(str(tmp_path)))


def gate(report, name):
    return next(g for g in report.gates if g.name == name)


@pytest.mark.parametrize("status", ["running", "queued", "error", "timeout", "failed"])
def test_unfinished_or_failed_lint_never_passes(status):
    rec = JobRecord("J-1", "lint", "fake", status=status, result={"diagnostics": []})
    assert not gate(evaluate("rtl_functional", Evidence(jobs=[rec])), "lint").ok


def test_latest_lint_failure_supersedes_old_pass():
    jobs = [JobRecord("J-1", "lint", "fake", status="passed", result={"diagnostics": []}),
            JobRecord("J-2", "lint", "fake", status="failed", result={"diagnostics": []})]
    assert not gate(evaluate("rtl_functional", Evidence(jobs=jobs)), "lint").ok


def test_latest_lec_failure_supersedes_old_pass():
    jobs = [JobRecord("J-1", "lec", "fake", status="passed", result={"status": "equivalent"}),
            JobRecord("J-2", "lec", "fake", status="failed", result={"status": "not-equivalent"})]
    assert not gate(evaluate("rtl_nfc", Evidence(jobs=jobs)), "lec").ok


@pytest.mark.parametrize("status", ["passed", "failed", "running", "error"])
@pytest.mark.parametrize("metrics", [{}, {"drc_violations": None, "setup_ws_ns": float("nan")}])
def test_missing_physical_metrics_cannot_certify_signoff(status, metrics):
    rec = JobRecord("J-1", "pnr", "librelane", status=status, result={"metrics": metrics})
    assert not evaluate("physical", Evidence(jobs=[rec])).all_passed


def test_adapter_rejects_missing_signoff_checks_and_failed_process(tmp_path):
    from chipchamp.adapters.librelane import LibreLaneAdapter, signoff_verdict
    from chipchamp.adapters.base import StepResult
    assert signoff_verdict({})[0] == "signoff-fail"
    assert signoff_verdict({"setup_ws_ns": "invalid"})[0] == "signoff-fail"
    metrics = tmp_path / "metrics.json"
    metrics.write_text("{}")
    plan = Plan("pnr", [], str(tmp_path), "librelane", artifacts={"metrics": str(metrics)})
    result = LibreLaneAdapter().parse(plan, [StepResult([], 1, "failed", "")])
    assert not result.ok


def test_edit_after_a_passing_job_requires_new_evidence(tmp_path):
    ctx = context(tmp_path)
    fs_write(ctx, "tb/tb_m.sv", "module tb_m; initial $finish; endmodule\n")
    src = str(tmp_path / "tb/tb_m.sv")
    plan = Plan("sim", [Step(["true"], str(tmp_path))], str(tmp_path), "review-pass")
    rec, _ = ctx.submit(plan, PassingAdapter(), input_files=[src])
    assert report_done(ctx)["accepted"]
    # An external edit is just as relevant as an fs.edit call.
    Path(src).write_text("NOT VALID HDL\n")
    result = report_done(ctx)
    assert not result["accepted"] and rec.id in result["stale_jobs"]


def test_target_additions_invalidate_prior_evidence(tmp_path):
    ctx = context(tmp_path)
    plan = Plan("lint", [Step(["true"], str(tmp_path))], str(tmp_path), "review-pass")
    rec, _ = ctx.submit(plan, PassingAdapter())
    (tmp_path / "rtl/new.sv").write_text("module new_module; endmodule")
    jobs, stale = ctx.evidence_jobs()
    assert not jobs and rec.id in stale


def test_external_header_edit_invalidates_derived_verdicts(tmp_path):
    ctx = context(tmp_path)
    (tmp_path / "rtl/m.sv").write_text('`include "config.svh"\nmodule m; endmodule')
    header = tmp_path / "rtl/config.svh"
    header.write_text('`define WIDTH 8\n')
    ctx.refresh_measurements()
    ctx.coverage_sets["default"] = CoverageService()
    ctx.sta_delta = {"new_violations": 0}
    ctx.riscv_cosim = {"match": True, "compared": 10}
    ctx.riscv_compliance = {"passed": 1, "total": 1, "failed": 0}
    header.write_text('`define WIDTH 16\n')
    ctx.evidence_jobs()
    assert not ctx.coverage_sets
    assert ctx.sta_delta is ctx.riscv_cosim is ctx.riscv_compliance is None


def test_diagnosis_can_cite_failure_before_the_fix(tmp_path):
    ctx = context(tmp_path)
    fs_write(ctx, "tb/tb_m.sv", "module tb_m; endmodule")
    old_hash = ctx.verification_hash()
    ctx.task_jobs.append(JobRecord("J-0001", "sim", "fake", status="failed",
                                   verification_hash=old_hash))
    ctx.task_notes.append({"kind": "root_cause", "evidence": "J-0001"})
    fs_write(ctx, "tb/tb_m.sv", "module tb_m; initial $finish; endmodule")
    plan = Plan("sim", [Step(["true"], str(tmp_path))], str(tmp_path), "review-pass")
    ctx.submit(plan, PassingAdapter())
    result = report_done(ctx)
    assert result["accepted"], result
    assert "J-0001" in result["stale_jobs"]


def test_symlink_alias_cannot_read_denied_or_write_protected_files(tmp_path):
    ctx = context(tmp_path)
    (tmp_path / "pdk").mkdir()
    (tmp_path / "pdk/private.txt").write_text("DUMMY_PRIVATE")
    (tmp_path / "public.txt").symlink_to(tmp_path / "pdk/private.txt")
    (ctx.ws.dot / "policy.yaml").write_text("unchanged")
    (tmp_path / "editable.txt").symlink_to(ctx.ws.dot / "policy.yaml")
    assert "error" in fs_read(ctx, "public.txt")
    assert "error" in fs_write(ctx, "editable.txt", "changed")
    assert "error" in fs_edit(ctx, "editable.txt", "unchanged", "changed")
    assert (ctx.ws.dot / "policy.yaml").read_text() == "unchanged"
    assert "public.txt" not in fs_list(ctx, pattern="*.txt")["files"]


def test_glob_expansion_and_symlink_search_cannot_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    ctx = context(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("DUMMY_OUTSIDE")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert not fs_grep(ctx, "DUMMY_OUTSIDE", glob="*/../../outside/*.txt")["hits"]
    assert not fs_grep(ctx, "DUMMY_OUTSIDE", glob="**/*.txt")["hits"]
    assert not fs_list(ctx, pattern=str(outside / "*.txt"))["files"]


def test_cache_tracks_transitive_includes_environment_and_workdir(tmp_path):
    src, header, nested = [tmp_path / name for name in ("m.sv", "config.svh", "bits.svh")]
    src.write_text('`include "config.svh"\nmodule m; endmodule')
    header.write_text('`include "bits.svh"\n')
    nested.write_text('`define WIDTH 8\n')
    plan = Plan("lint", [Step(["true", str(src)], str(tmp_path))], str(tmp_path), "review-pass")
    adapter = PassingAdapter()
    key = lambda: cache_key(plan, adapter, inputs_hash="unchanged")
    first = key()
    assert first
    nested.write_text('`define WIDTH 16\n')
    second = key()
    assert second != first
    plan.steps[0].env["MODE"] = "new"
    third = key()
    assert third != second
    (tmp_path / "another").mkdir()
    plan.steps[0].cwd = str(tmp_path / "another")
    assert key() != third


def test_cache_disabled_for_unresolved_dependencies(tmp_path):
    src = tmp_path / "m.sv"
    src.write_text('`include `GENERATED_HEADER\nmodule m; endmodule')
    plan = Plan("lint", [Step(["true", str(src)], str(tmp_path))], str(tmp_path), "review-pass")
    assert cache_key(plan, PassingAdapter(), inputs_hash="unchanged") == ""


def test_header_change_runs_job_again_instead_of_reusing_verdict(tmp_path):
    ctx = context(tmp_path)
    src = tmp_path / "rtl/m.sv"
    header = tmp_path / "rtl/config.svh"
    src.write_text('`include "config.svh"\nmodule m; endmodule')
    header.write_text('`define WIDTH 8\n')
    plan = Plan("lint", [Step(["true", str(src)], str(tmp_path))], str(tmp_path), "review-pass")
    first, _ = ctx.submit(plan, PassingAdapter())
    hit, _ = ctx.submit(plan, PassingAdapter())
    assert hit.cached and first.id == hit.id
    header.write_text('`define WIDTH 16\n')
    rerun, _ = ctx.submit(plan, PassingAdapter())
    assert not rerun.cached and rerun.id != first.id


def test_cache_tracks_memory_image(tmp_path):
    src = tmp_path / "tb.sv"
    src.write_text('initial $readmemh("rom.hex", mem);')
    data = tmp_path / "rom.hex"
    data.write_text("00")
    plan = Plan("sim", [Step(["true", str(src)], str(tmp_path))], str(tmp_path), "review-pass")
    before = cache_key(plan, PassingAdapter(), inputs_hash="same")
    data.write_text("ff")
    assert before and cache_key(plan, PassingAdapter(), inputs_hash="same") != before


def test_combining_fpga_and_riscv_changes_preserves_all_obligations():
    policy = PolicyEngine(riscv_core=True)
    diff = Diff(files=[FileChange("rtl/core.sv", "modified", "a", "b"),
                       FileChange("pins.xdc", "modified", "c", "d")])
    expected = {"lint", "smoke_sim", "isa_cosim", "isa_compliant", "no_gaming", "fpga_routed"}
    assert expected <= set(policy.check(diff).required_gates)
    assert expected <= {g.name for g in policy.validate_done(diff, Evidence()).gates}


def test_passing_different_seed_does_not_erase_failure():
    jobs = [JobRecord("J-1", "sim", "fake", status="failed", seed=1, input_files=["tb/x.sv"]),
            JobRecord("J-2", "sim", "fake", status="passed", seed=2, input_files=["tb/x.sv"])]
    assert not gate(evaluate("rtl_functional", Evidence(jobs=jobs)), "affected_regress").ok
    jobs.append(JobRecord("J-3", "sim", "fake", status="passed", seed=1, input_files=["tb/x.sv"]))
    assert gate(evaluate("rtl_functional", Evidence(jobs=jobs)), "affected_regress").ok


def test_omitted_regression_test_blocks_completion():
    jobs = [JobRecord("J-1", "sim", "fake", status="passed", seed=1, test_name="smoke")]
    ev = Evidence(jobs=jobs, expected_sims=[{"test": "smoke", "seed": 1},
                                         {"test": "stress", "seed": 1}])
    result = gate(evaluate("rtl_functional", ev), "affected_regress")
    assert not result.ok and "stress" in result.detail


def test_riscv_wrong_destination_register_is_a_divergence():
    from chipchamp.riscv.trace import TraceStep, diff_traces
    dut = TraceStep(pc=0, insn=0x500093, reg="x2", value=5)
    ref = TraceStep(pc=0, insn=0x500093, reg="x1", value=5)
    result = diff_traces([dut], [ref])
    assert result is not None and result.reason == "writeback"


@pytest.mark.parametrize("change", ["removed", "weight", "section", "kind"])
def test_coverage_denominator_cannot_be_changed_to_improve_result(change):
    baseline = CoverageModel()
    baseline.add_bin(Bin("hit", "g", "p", "hit", count=1, section="s"))
    baseline.add_bin(Bin("miss", "g", "p", "miss", count=0, section="s"))
    current = CoverageModel.from_dict(baseline.to_dict())
    if change == "removed":
        del current.bins["miss"]
    else:
        setattr(current.bins["miss"], change, 0 if change == "weight" else "changed")
    verdict = CoverageService(current).baseline_check(baseline)
    assert not verdict.ok and verdict.model_changes


def test_detached_worker_obeys_parent_license_pool(tmp_path):
    import json
    from _detached_fake import SlowOk
    from chipchamp.jobs.farm import FarmConfig
    runner = JobRunner(str(tmp_path / "runs"), None, licenses={"seat": 1}, license_wait_s=0.1,
                       farm=FarmConfig(kind="slurm", queue="licensed"))
    assert runner.licenses.acquire(["seat"])
    try:
        plan = Plan("sim", [Step(["true"], str(tmp_path))], str(tmp_path), "slowok",
                    license_features=["seat"])
        rec = runner.submit_async(plan, SlowOk())
        done = runner.wait(rec.id, timeout=10)
        assert done.status == "error" and "no license seat" in done.summary
        launch = json.loads((tmp_path / "runs" / rec.id / "launch.json").read_text())
        assert launch["runner_kwargs"]["farm"]["queue"] == "licensed"
        assert launch["kwargs"]["no_cache"] is True
    finally:
        runner.licenses.release(["seat"])


def test_detached_completion_updates_task_and_bills_once(tmp_path):
    from _detached_fake import SlowOk
    from chipchamp.tools.verif_tools import job_wait
    ctx = context(tmp_path)
    ctx.record_edit("tb/tb_m.sv", "old", "new", "modified")
    plan = Plan("sim", [Step(["sleep", "0.1"], str(tmp_path))], str(tmp_path), "slowok")
    rec, _ = ctx.submit(plan, SlowOk(), detach=True, is_v3=True)
    assert ctx.ledger.v3_used == 1
    assert job_wait(ctx, rec.id, timeout_s=10)["status"] == "passed"
    assert ctx.task_jobs[0].status == "passed"
    assert not ctx.pending_jobs and ctx.ledger.cpu_seconds > 0
    cost = ctx.ledger.cpu_seconds
    job_wait(ctx, rec.id, timeout_s=10)
    assert ctx.ledger.cpu_seconds == cost
    assert report_done(ctx)["accepted"]


def test_parallel_budget_reservations_and_billing_are_atomic():
    from concurrent.futures import ThreadPoolExecutor
    from chipchamp.policy.budgets import Budget, BudgetLedger
    ledger = BudgetLedger(Budget(v3_submissions=1))
    with ThreadPoolExecutor(max_workers=8) as workers:
        allowed = list(workers.map(lambda _: ledger.reserve_v3()[0], range(32)))
        rec = JobRecord("J-1", "sim", "fake", cpu_seconds=2, license_seconds=3)
        list(workers.map(lambda _: ledger.record_job(rec), range(32)))
    assert sum(allowed) == 1 and ledger.v3_used == 1
    assert ledger.cpu_seconds == 2 and ledger.license_seconds == 3


@pytest.mark.parametrize("nested", [False, True])
def test_mr_does_not_commit_or_modify_users_staged_files(tmp_path, nested):
    from test_security_mr import _git_workspace
    from chipchamp.tools.meta_tools import mr_prepare
    ws = _git_workspace(tmp_path)
    if nested:
        child = Path(ws.root) / "ip"
        (child / ".chipchamp").mkdir(parents=True)
        for filename in ("config.toml", "policy.yaml"):
            (child / ".chipchamp" / filename).write_bytes((ws.dot / filename).read_bytes())
        ws = Workspace(str(child))
    ctx = ToolContext(ws)
    root = Path(ws.root)
    def git(*args):
        return subprocess.check_output(["git", "-C", ws.root, *args])
    (root / "unrelated.txt").write_text("user staged work")
    git("add", "unrelated.txt")
    index_before = git("diff", "--cached", "--binary")
    head_before = git("rev-parse", "HEAD")
    fs_write(ctx, "docs/change.md", "task change\n")
    result = mr_prepare(ctx, "fix/review", "Reviewed change")
    assert "error" not in result, result
    expected = b"ip/docs/change.md" if nested else b"docs/change.md"
    assert git("diff-tree", "--no-commit-id", "--name-only", "-r", "fix/review").splitlines() == [expected]
    assert git("diff", "--cached", "--binary") == index_before
    assert git("rev-parse", "HEAD") == head_before
    assert (root / "docs/change.md").read_text() == "task change\n"


def test_mr_refuses_task_file_changed_by_human_after_edit(tmp_path):
    from test_security_mr import _git_workspace
    from chipchamp.tools.meta_tools import mr_prepare
    ws = _git_workspace(tmp_path)
    ctx = ToolContext(ws)
    fs_write(ctx, "docs/change.md", "reviewed task content\n")
    path = Path(ws.root) / "docs/change.md"
    path.write_text("human changes after the edit\n")
    result = mr_prepare(ctx, "fix/stale", "Stale edit")
    assert "changed after the recorded edit" in result["error"]
    assert path.read_text() == "human changes after the edit\n"
    assert subprocess.run(["git", "-C", ws.root, "show-ref", "--verify", "--quiet",
                           "refs/heads/fix/stale"]).returncode == 1


def test_multibit_w1c_sets_and_clears_only_selected_bits(tmp_path):
    from conftest import has
    if not has("iverilog"):
        pytest.skip("iverilog not installed")
    from chipchamp.regmap.compiler import Field, Register, RegmapSpec, generate
    spec = RegmapSpec(name="multibit", registers=[Register("STATUS", 0, fields=[Field("pending", 3, 0, "W1C")])])
    for name, content in generate(spec).items():
        (tmp_path / name).write_text(content)
    tb = tmp_path / "tb.sv"
    tb.write_text('''module tb;
reg clk=0; always #5 clk=~clk;
reg rstn=0, psel=0, penable=0, pwrite=0;
reg [11:0] paddr=0; reg [31:0] pwdata=0; reg [3:0] hwset=0;
wire [31:0] prdata; wire pready, pslverr;
multibit_reg_top dut(.pclk(clk),.presetn(rstn),.psel(psel),.penable(penable),
 .pwrite(pwrite),.paddr(paddr),.pwdata(pwdata),.prdata(prdata),.pready(pready),
 .pslverr(pslverr),.hwif_status_pending_set_i(hwset));
initial begin
 #12; rstn=1; hwset=4'b0101; #10; hwset=0; #1;
 if (prdata[3:0] !== 4'b0101) $fatal(1,"selective set");
 #9; psel=1; penable=1; pwrite=1; pwdata=1; #10; pwrite=0; #1;
 if (prdata[3:0] !== 4'b0100) $fatal(1,"selective clear");
 #9; pwrite=1; pwdata=4; hwset=4'b0101; #10; pwrite=0; hwset=0; #1;
 if (prdata[3:0] !== 4'b0101) $fatal(1,"hardware set priority");
 $finish;
end
endmodule
''')
    binary = tmp_path / "sim"
    subprocess.run(["iverilog", "-g2012", "-s", "tb", "-o", str(binary),
                    str(tmp_path / "multibit_reg_top.sv"), str(tb)], check=True, capture_output=True)
    subprocess.run(["vvp", str(binary)], check=True, capture_output=True, timeout=10)
