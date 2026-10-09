"""Coverage evidence survives sessions and only prioritizes the selected matrix."""
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from chipchamp.adapters.base import Adapter, Plan, Step, ToolResult
from chipchamp.adapters.verilator import VerilatorAdapter
from chipchamp.cli import cli
from chipchamp.config import Workspace
from chipchamp.coverage.history import CoverageHistory
from chipchamp.tools.context import ToolContext
from chipchamp.tools.verif_tools import cov_run, cov_history, cov_compare, regress_run
from chipchamp.tools.verification_tools import verify_plan
from test_verification_planning import design, fake_planner  # noqa: F401


class CoverageAdapter(Adapter):
    name = "coverage-fixture"

    def available(self):
        return True

    def version(self):
        return "fixture-1"

    def coverage(self, files, tb_top, workdir, **kwargs):
        artifact = str(Path(kwargs["output_dir"]) / "coverage.json")
        source = str(Path(workdir) / "coverage-input.json")
        code = "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])"
        return Plan("coverage", [Step([sys.executable, "-c", code, source, artifact], workdir)],
                    workdir, self.name, artifacts={"coverage": artifact})

    def parse(self, plan, results):
        return ToolResult(all(r.rc == 0 for r in results), "coverage", self.name,
                          artifacts=plan.artifacts)


@pytest.fixture
def coverage_adapter(design, monkeypatch):
    original = design.registry.for_role
    monkeypatch.setattr(design.registry, "for_role", lambda role:
                        CoverageAdapter() if role == "coverage" else original(role))
    return design


def observation(ctx, bins=None, test="focused", seed=1, source="rtl/leaf.sv:1"):
    bins = {"a": 1, "b": 0} if bins is None else bins
    (Path(ctx.ws.root) / "coverage-input.json").write_text(json.dumps({"covergroups": [{
        "name": "dut", "source": source, "coverpoints": [{"name": "cp", "bins": [
            {"name": name, "hits": hits} for name, hits in bins.items()]}]}]}))
    result = cov_run(ctx, test, seed=seed)
    assert "error" not in result, result
    return result


def test_history_survives_context_and_detects_lost_hits_and_removed_bins(coverage_adapter):
    ctx = coverage_adapter
    a = observation(ctx, {"a": 1, "b": 0, "gone": 0})
    b = observation(ctx, {"a": 0, "b": 1})
    assert a["job"] != b["job"]
    fresh = ToolContext(Workspace(ctx.ws.root))
    history = cov_history(fresh, test="focused", seed=1)
    assert history["total"] == 2 and not history["invalid"], history
    assert history["observations"][0]["current"]
    delta = cov_compare(fresh, a["job"], b["job"])
    assert delta["newly_covered"]["bins"] == ["dut::cp::b"]
    assert delta["newly_uncovered"]["bins"] == ["dut::cp::a"]
    assert delta["removed_bins"]["count"] == 1
    assert not delta["baseline_check"]["ok"]  # the percentage rose by removing a hole
    assert b["delta"] == delta
    cli_result = CliRunner().invoke(cli, ["--root", ctx.ws.root, "--json", "cov-history", "--test", "focused"])
    assert cli_result.exit_code == 0 and json.loads(cli_result.output)["total"] == 2, cli_result.output
    diff = CliRunner().invoke(cli, ["--root", ctx.ws.root, "--json", "cov-diff", a["job"], b["job"]])
    assert diff.exit_code == 0 and json.loads(diff.output)["removed_bins"]["count"] == 1, diff.output


@pytest.mark.parametrize("change", ["observation", "artifact", "log"])
def test_history_rejects_tampered_evidence(coverage_adapter, change):
    ctx = coverage_adapter
    a = observation(ctx)
    rec = ctx.runner.get(a["job"])
    path = {"observation": Path(ctx.ws.root) / rec.coverage_observation["path"],
            "artifact": Path(rec.artifacts["coverage"]), "log": Path(rec.steps[0].log_path)}[change]
    path.write_text(path.read_text() + "\nmodified")
    history = cov_history(ctx)
    assert history["total"] == 0 and len(history["invalid"]) == 1
    assert "error" in cov_compare(ctx, a["job"], a["job"])
    assert CoverageHistory(ctx).order(["unrelated", "focused"], ["rtl/leaf.sv"])["tests"] == ["unrelated", "focused"]


def test_history_is_separate_per_seed_and_test(coverage_adapter):
    ctx = coverage_adapter
    a = observation(ctx)
    b = observation(ctx, seed=2)
    c = observation(ctx, test="unrelated")
    assert cov_history(ctx, test="focused", seed=2)["total"] == 1
    for other in (b, c):
        assert "same target, test, seed" in cov_compare(ctx, a["job"], other["job"])["error"]


def test_failed_and_empty_coverage_never_enter_history(coverage_adapter, monkeypatch):
    ctx = coverage_adapter
    with pytest.raises(AssertionError, match="nonempty"):
        observation(ctx, {})
    monkeypatch.setattr(CoverageAdapter, "parse", lambda self, plan, results:
                        ToolResult(False, "coverage", self.name, artifacts=plan.artifacts))
    assert "error" in cov_run(ctx, "focused")
    assert cov_history(ctx)["total"] == 0


def test_history_orders_full_matrix_after_rtl_edit(coverage_adapter, fake_planner, monkeypatch):
    ctx = coverage_adapter
    observation(ctx, {"a": 0})
    b = observation(ctx, {"a": 1, "b": 1}, test="unrelated")
    from chipchamp.tools.fs_tools import fs_edit
    fs_edit(ctx, "rtl/leaf.sv", "endmodule", "endmodule // changed")
    order = CoverageHistory(ctx).order(["focused", "unrelated"], ["rtl/leaf.sv"])
    assert order["tests"] == ["unrelated", "focused"] and order["method"] == "coverage_history"
    assert order["reasons"]["unrelated"]["jobs"] == [b["job"]]
    assert not cov_history(ctx)["observations"][0]["current"]
    monkeypatch.setattr("chipchamp.tools.verif_tools.self_parallel", lambda ctx: 1)
    result = regress_run(ctx)
    assert result["selected_tests"] == ["unrelated", "focused"], result
    assert result["passed"] == result["total"] == 4, result
    assert sorted((r.test_name, r.seed) for r in ctx.task_jobs if r.kind == "sim") == [
        ("focused", 1), ("focused", 2), ("unrelated", 1), ("unrelated", 2)]
    assert [r.test_name for r in ctx.task_jobs if r.kind == "sim"] == ["unrelated", "unrelated", "focused", "focused"]
    resumed = regress_run(ctx, resume=result["checkpoint"])
    assert resumed["resumed"] == 4 and resumed["ordering"] == result["ordering"], resumed


def test_planner_retains_selection_and_ignores_changed_test_definition(coverage_adapter):
    ctx = coverage_adapter
    observation(ctx)
    plan = verify_plan(ctx, changed_paths=["rtl/leaf.sv"])
    assert plan["ordering"]["tests"] == plan["selection"]["selected_tests"] == ["focused"]
    assert plan["ordering"]["method"] == "coverage_lines"
    path = ctx.ws.dot / "tests.yaml"
    path.write_text(path.read_text().replace("tb_focus", "tb_changed"))
    ctx.ws.invalidate(ctx.target_name)
    order = CoverageHistory(ctx).order(["unrelated", "focused"], ["rtl/leaf.sv"])
    assert order["method"] == "declared_order"


def test_live_verilator_coverage_records_seed_and_fresh_artifacts(design):
    if not VerilatorAdapter().available():
        pytest.skip("Verilator unavailable")
    first = cov_run(design, "focused", seed=17)
    assert "error" not in first and first["observation"]["seed"] == 17, first
    assert first["summary"]["bins"] > 0
    second = cov_run(design, "focused", seed=17)
    assert "error" not in second and second["delta"]["delta_percent"] == 0, second
    assert design.runner.get(first["job"]).artifacts != design.runner.get(second["job"]).artifacts
