"""Saved verification plans, incremental checkpoints and dependency provenance."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from chipchamp.adapters.base import Plan, Step, ToolResult
from chipchamp.adapters.icarus import IcarusAdapter
from chipchamp.adapters.verilator import VerilatorAdapter
from chipchamp.cli import cli
from chipchamp.config import Workspace
from chipchamp.jobs.checkpoint import case_key, run_lock
from chipchamp.jobs.inputs import capture_inputs, finish_capture, snapshot_current
from chipchamp.jobs.record import JobRecord
from chipchamp.jobs.reports import sarif_report
from chipchamp.tools.context import ToolContext
from chipchamp.tools.verif_tools import regress_run
from test_verification_planning import design, fake_planner, LocalAdapter  # noqa: F401


def checkpoint(ctx, result):
    path = Path(ctx.ws.root) / result["checkpoint"]
    return path, json.loads(path.read_text())


def test_resume_across_contexts_preserves_passes_and_budget(design, fake_planner):
    first = regress_run(design)
    assert first["passed"] == 4, first
    path, state = checkpoint(design, first)
    assert state["state"] == "finished" and len(state["cases"]) == 4
    ctx = ToolContext(Workspace(design.ws.root))
    ctx.ledger.budget.v3_submissions = 4
    resumed = regress_run(ctx, resume=first["checkpoint"])
    assert resumed["resumed"] == 4 and resumed["submitted"] == 0, resumed
    assert ctx.ledger.v3_used == 4 and len(ctx.expected_sims) == 4
    assert len(ctx.task_jobs) == 4 and all(r.cached for r in ctx.task_jobs)
    assert regress_run(ctx, resume=first["checkpoint"])["resumed"] == 4
    assert ctx.ledger.v3_used == 4


def test_missing_cases_are_retried_and_checkpoint_written_per_case(design, fake_planner, monkeypatch):
    first = regress_run(design, seeds=[1])
    path, state = checkpoint(design, first)
    del state["cases"][case_key("unrelated", 1)]
    state["state"] = "running"
    path.write_text(json.dumps(state))
    from chipchamp.jobs.checkpoint import Checkpoint
    recorded = []
    original = Checkpoint.record
    def record(self, test, seed, rec):
        original(self, test, seed, rec)
        assert json.loads(self.path.read_text())["cases"][case_key(test, seed)]["job"] == rec.id
        recorded.append((test, seed))
    monkeypatch.setattr(Checkpoint, "record", record)
    resumed = regress_run(design, resume=first["checkpoint"])
    assert resumed["resumed"] == 1 and resumed["submitted"] == 1, resumed
    assert recorded == [("unrelated", 1)]


def test_resume_retries_failed_cases_even_when_failure_is_cached(design, fake_planner, monkeypatch):
    original = LocalAdapter.parse
    monkeypatch.setattr(LocalAdapter, "parse", lambda self, plan, results:
        ToolResult(ok=plan.meta["test_name"] != "focused", kind="sim", adapter=self.name,
                   status="fail" if plan.meta["test_name"] == "focused" else "pass"))
    first = regress_run(design, seeds=[1])
    assert first["failed"] == 1 and first["passed"] == 1, first
    path, state = checkpoint(design, first)
    state["cases"][case_key("focused", 1)]["status"] = "passed"  # never trust the checkpoint verdict
    path.write_text(json.dumps(state))
    monkeypatch.setattr(LocalAdapter, "parse", original)
    second = regress_run(design, resume=first["checkpoint"])
    assert second["passed"] == 2 and second["resumed"] == 1, second
    assert design.ledger.v3_used == 3


@pytest.mark.parametrize("change", ["log", "inputs", "environment", "version"])
def test_resume_invalidates_changed_evidence_and_execution(design, fake_planner, monkeypatch, change):
    first = regress_run(design, tests=["focused"], seeds=[1])
    rec = design.task_jobs[-1]
    if change == "log":
        Path(rec.steps[0].log_path).unlink()
    elif change == "inputs":
        path = Path(design.ws.root) / "rtl/leaf.sv"
        path.write_text(path.read_text() + "// changed\n")
    elif change == "environment":
        monkeypatch.setenv("CHIPCHAMP_TEST_INPUT", "changed")
    else:
        monkeypatch.setattr(LocalAdapter, "version", lambda self: "2")
    second = regress_run(design, resume=first["checkpoint"])
    assert second["resumed"] == 0 and second["passed"] == 1, second
    assert design.task_jobs[-1].id != rec.id


def test_resume_budget_errors_do_not_disappear(design, fake_planner):
    design.ledger.budget.v3_submissions = 1
    first = regress_run(design, seeds=[1])
    ctx = ToolContext(Workspace(design.ws.root))
    ctx.ledger.budget.v3_submissions = 1
    second = regress_run(ctx, resume=first["checkpoint"])
    assert second["resumed"] == 1 and second["errored"] == 1, second
    assert second["total"] == 2


def test_concurrent_resume_is_rejected(design, fake_planner):
    first = regress_run(design, seeds=[1])
    path, _ = checkpoint(design, first)
    with run_lock(path.parent):
        result = regress_run(design, resume=first["checkpoint"])
    assert "already running" in result["error"]


@pytest.mark.parametrize("kwargs", [
    {"resume": "../escape.json"}, {"resume": "random.json"},
    {"resume": "a.json", "seeds": [1]}, {"plan_file": "a.json", "affected": True},
    {"resume": "a.json", "plan_file": "b.json"}, {"sarif": "rtl/leaf.sv"},
])
def test_invalid_saved_run_requests_do_not_launch(design, kwargs):
    assert "error" in regress_run(design, **kwargs)
    assert not design.task_jobs


def test_saved_plan_cli_round_trip_and_stale_rejection(design, fake_planner):
    args = ["--root", design.ws.root, "--json"]
    runner = CliRunner()
    saved = runner.invoke(cli, args + ["verify-plan", "--changed", "rtl/leaf.sv", "--out", "work/plan.json"])
    assert saved.exit_code == 0, saved.output
    run = runner.invoke(cli, args + ["regress", "--plan", "work/plan.json"])
    assert run.exit_code == 0, run.output
    result = json.loads(run.output)
    assert result["passed"] == 2 and result["selected_tests"] == ["focused"]
    resumed = runner.invoke(cli, args + ["regress", "--resume", result["checkpoint"]])
    assert resumed.exit_code == 0 and json.loads(resumed.output)["resumed"] == 2, resumed.output
    path = Path(design.ws.root) / "tb/tb_other.sv"
    path.write_text(path.read_text() + "// new dependency\n")
    stale = runner.invoke(cli, args + ["regress", "--plan", "work/plan.json"])
    assert stale.exit_code == 1 and "stale" in stale.output


@pytest.mark.parametrize("adapter,flag,source", [("icarus", "-f", "rtl/top.sv"),
                                                ("verilator", "-f", "rtl/top.sv"),
                                                ("verilator", "-F", "../rtl/top.sv")])
def test_nested_native_filelists_headers_and_new_files(tmp_path, adapter, flag, source):
    (tmp_path / "lists").mkdir()
    (tmp_path / "rtl").mkdir()
    src = tmp_path / "rtl/top.sv"
    src.write_text('`include "defs.svh"\nmodule top; endmodule')
    header = tmp_path / "rtl/defs.svh"
    header.write_text("`define FOO 1\n")
    outer, inner = tmp_path / "lists/outer.f", tmp_path / "lists/inner.f"
    nested = "inner.f" if flag == "-F" else "lists/inner.f"
    outer.write_text(f"{flag} {nested}\n")
    inner.write_text(source + "\n")
    plan = Plan("sim", [Step([adapter, flag, "lists/outer.f"], str(tmp_path))], str(tmp_path), adapter)
    before = capture_inputs(plan)
    assert before["complete"] and set(before["files"]) == set(map(str, (src, header, inner, outer))), before
    assert snapshot_current(before)
    header.write_text("`define FOO 2\n")
    assert not snapshot_current(before)
    before = capture_inputs(plan)
    inner.write_text(source + "\nmissing.sv\n")
    assert not snapshot_current(before)


def test_unknown_filelist_cycles_and_dynamic_dependencies_fail_closed(tmp_path):
    (tmp_path / "loop.f").write_text("-f loop.f\n")
    p = Plan("sim", [Step(["iverilog", "-f", "loop.f"], str(tmp_path))], str(tmp_path), "icarus")
    assert not capture_inputs(p)["complete"]
    (tmp_path / "loop.f").write_text("$ROOT/top.sv\n")
    assert not capture_inputs(p)["complete"]


def test_cocotb_local_helpers_data_and_new_files_are_captured(tmp_path):
    tb = tmp_path / "tb"
    tb.mkdir()
    (tb / "test_top.py").write_text("from helper import value\n")
    helper = tb / "helper.py"
    helper.write_text("value = 1\n")
    (tb / "stimulus.bin").write_bytes(b"\x01")
    p = Plan("sim", [], str(tmp_path), "cocotb", meta={"input_dirs": [str(tb)]})
    before = capture_inputs(p)
    assert len(before["files"]) == 3 and not before["complete"]
    assert snapshot_current(before)
    (tb / "new_helper.py").write_text("value = 2\n")
    assert not snapshot_current(before)


def test_generated_binary_is_an_output_not_a_future_input(tmp_path):
    src = tmp_path / "top.sv"
    src.write_text("module top; endmodule")
    p = VerilatorAdapter().sim([str(src)], "top", str(tmp_path), waves=False)
    before = capture_inputs(p, [str(src)])
    binary = Path(p.artifacts["executable"])
    binary.parent.mkdir()
    binary.write_bytes(b"generated binary")
    assert before == capture_inputs(p, [str(src)])


def test_input_change_during_execution_is_never_reusable(tmp_path):
    src = tmp_path / "top.sv"
    src.write_text("module top; endmodule")
    p = Plan("sim", [], str(tmp_path), "icarus")
    before = capture_inputs(p, [str(src)])
    src.write_text("module changed; endmodule")
    done = finish_capture(p, before, [str(src)])
    assert done["unstable"] and not done["complete"] and not snapshot_current(done)


def test_live_icarus_resume_isolated_seeds_and_artifact_tamper(design):
    if not IcarusAdapter().available():
        pytest.skip("iverilog not installed")
    first = regress_run(design, tests=["focused"])
    assert first["passed"] == 2, first
    a, b = design.task_jobs
    assert a.artifacts["vvp"] != b.artifacts["vvp"]
    assert a.input_snapshot["observed"] and a.execution_key, a.input_snapshot
    second = regress_run(design, resume=first["checkpoint"])
    assert second["resumed"] == 2, second
    Path(b.artifacts["vvp"]).write_bytes(b"corrupted")
    third = regress_run(design, resume=first["checkpoint"])
    assert third["resumed"] == 1 and third["passed"] == 2, third


def test_live_verilator_dependency_manifest_and_resume(design):
    if not VerilatorAdapter().available():
        pytest.skip("verilator not installed")
    config = design.ws.dot / "config.toml"
    config.write_text(config.read_text().replace('sim_inner = "icarus"', 'sim_inner = "verilator"'))
    ctx = ToolContext(Workspace(design.ws.root))
    first = regress_run(ctx, tests=["focused"], seeds=[1])
    assert first["passed"] == 1, first
    rec = ctx.task_jobs[-1]
    assert rec.input_snapshot["observed"] and rec.execution_key, rec.input_snapshot
    second = regress_run(ctx, resume=first["checkpoint"])
    assert second["resumed"] == 1, second


def test_sarif_lint_locations_and_infrastructure_errors(tmp_path):
    rec = JobRecord("J-1", "lint", "verilator", status="failed", result={"diagnostics": [
        {"code": "WIDTH", "severity": "warning", "message": "wrong width", "file": "rtl/a b.sv", "line": 7},
        {"severity": "error", "message": "external", "file": "/outside/secret.sv", "line": 2}]})
    error = JobRecord("ERR-t-1", "sim", "icarus", status="error", summary="budget exhausted")
    report = sarif_report([rec, error], str(tmp_path))
    run = report["runs"][0]
    first = run["results"][0]
    location = first["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "rtl/a%20b.sv"
    assert location["region"]["startLine"] == 7
    assert "locations" not in run["results"][1]
    assert run["results"][-1]["ruleId"] == "icarus/JOB_ERROR"
    assert run["invocations"][-1]["executionSuccessful"] is False


def test_runner_provenance_restores_cost_after_lost_checkpoint_callback(design, fake_planner):
    first = regress_run(design, seeds=[1])
    path, state = checkpoint(design, first)
    state["cases"] = {}  # Simulate dying after the job persisted, before the callback.
    state["charged_jobs"] = []
    path.write_text(json.dumps(state))
    ctx = ToolContext(Workspace(design.ws.root))
    ctx.ledger.budget.v3_submissions = 2
    resumed = regress_run(ctx, resume=first["checkpoint"])
    assert resumed["errored"] == 2 and ctx.ledger.v3_used == 2, resumed


def test_retry_uses_new_attempt_directory(design):
    if not IcarusAdapter().available():
        pytest.skip("iverilog not installed")
    first = regress_run(design, tests=["focused"], seeds=[1])
    path, state = checkpoint(design, first)
    old_attempt = state["cases"][case_key("focused", 1)]["attempt"]
    old_binary = Path(design.task_jobs[-1].artifacts["vvp"])
    old_binary.write_bytes(b"old simulator still owns this directory")
    resumed = regress_run(design, resume=first["checkpoint"])
    assert resumed["passed"] == 1 and resumed["resumed"] == 0
    _, state = checkpoint(design, resumed)
    assert state["cases"][case_key("focused", 1)]["attempt"] != old_attempt
    assert old_binary.read_bytes() == b"old simulator still owns this directory"


def test_explicit_filelist_updates_live_target_and_job_manifest(design):
    import yaml
    root = Path(design.ws.root)
    (root / "files.f").write_text("-f rtl/rtl.f\n")
    nested = root / "rtl/rtl.f"
    nested.write_text("leaf.sv\nwrapper.sv\nother.sv\n+define+MODE=1\n")
    config = design.ws.dot / "config.toml"
    config.write_text(config.read_text().replace('globs = ["rtl/*.sv"]', 'filelists = ["files.f"]'))
    ctx = ToolContext(Workspace(str(root)))
    nested.write_text("leaf.sv\nwrapper.sv\n+define+MODE=2\n")
    ctx.ws.invalidate(ctx.target_name)
    assert ctx.target.defines["MODE"] == "2" and len(ctx.target.sources) == 2
    assert set(ctx.target.fileset.manifests) == {str(root / "files.f"), str(nested)}
    inputs = root / "stimulus.hex"
    inputs.write_text("00\n")
    manifest = ctx.ws.dot / "tests.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["tests"][0]["runner"]["inputs"] = ["stimulus.hex"]
    manifest.write_text(yaml.safe_dump(data))
    if not IcarusAdapter().available():
        pytest.skip("iverilog not installed")
    first = regress_run(ctx, tests=["focused"], seeds=[1])
    assert first["passed"] == 1, first
    assert str(inputs) in ctx.task_jobs[-1].input_snapshot["files"]
    inputs.write_text("01\n")
    assert regress_run(ctx, resume=first["checkpoint"])["resumed"] == 0


def test_ci_exports_lint_and_budget_errors_with_nonzero_exit(design, fake_planner, monkeypatch):
    from chipchamp.tools import all_tools
    design.ledger.budget.v3_submissions = 0
    def lint(ctx):
        rec = JobRecord("J-lint", "lint", "verilator", status="passed", result={"diagnostics": [
            {"severity": "warning", "code": "WIDTH", "message": "width", "file": "rtl/leaf.sv", "line": 1}]})
        ctx.task_jobs.append(rec)
        return {"job": rec.id, "status": "passed", "errors": 0, "warnings": 1}
    monkeypatch.setattr(all_tools()["lint.run"], "handler", lint)
    monkeypatch.setattr("chipchamp.cli.verify._ctx", lambda *args: design)
    monkeypatch.setattr("chipchamp.dashboard.generate_dashboard", lambda *args: None)
    result = CliRunner().invoke(cli, ["--root", design.ws.root, "--json", "ci", "--tag", ""])
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    sarif = json.loads((Path(design.ws.root) / data["reports"]["sarif"]).read_text())
    findings = sarif["runs"][0]["results"]
    assert len(findings) == 3 and findings[0]["ruleId"] == "verilator/WIDTH"
    assert all("budget" in f["message"]["text"] for f in findings[1:])


def test_literal_file_io_and_foreign_runtime_dependencies(tmp_path):
    src = tmp_path / "tb.sv"
    data = tmp_path / "stimulus.txt"
    data.write_text("1\n")
    src.write_text('module tb; integer fd; initial fd = $fopen("stimulus.txt", "r"); endmodule')
    p = Plan("sim", [], str(tmp_path), "icarus")
    # Runtime paths are relative to the simulation step's working directory.
    p.steps = [Step(["vvp", "generated.vvp"], str(tmp_path))]
    before = capture_inputs(p, [str(src)])
    assert before["complete"] and str(data) in before["files"]
    data.write_text("2\n")
    assert not snapshot_current(before)
    src.write_text('module tb; import "DPI-C" function int query(); endmodule')
    assert not capture_inputs(p, [str(src)])["complete"]


def test_live_verilator_lint_captures_native_dependencies(design):
    if not VerilatorAdapter().available():
        pytest.skip("verilator not installed")
    from chipchamp.tools.build_tools import lint_run
    result = lint_run(design)
    assert "error" not in result, result
    rec = design.task_jobs[-1]
    assert rec.input_snapshot["observed"], rec.input_snapshot


def test_saved_plan_scope_is_independent_of_session_edit_history(design, fake_planner):
    from chipchamp.tools.fs_tools import fs_edit
    from chipchamp.tools.verification_tools import verify_plan
    fs_edit(design, "tb/tb_focus.sv", "#1", "#2")
    plan = verify_plan(design, changed_paths=["tb/tb_focus.sv"], seeds=[1])
    saved = Path(design.ws.root) / "plan.json"
    saved.write_text(json.dumps(plan))
    fresh = ToolContext(Workspace(design.ws.root))
    result = regress_run(fresh, plan_file="plan.json")
    assert result.get("passed") == 1, result
