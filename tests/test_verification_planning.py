"""Change-impact planning and portable regression reports, across public surfaces."""
from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from chipchamp.adapters.base import Adapter, Plan, Step, ToolResult
from chipchamp.cli import cli
from chipchamp.config import Workspace
from chipchamp.jobs.record import JobRecord
from chipchamp.jobs.regression import RegressionManager
from chipchamp.jobs.reports import junit_xml
from chipchamp.tools import all_tools
from chipchamp.tools.context import ToolContext
from chipchamp.tools.fs_tools import fs_edit
from chipchamp.tools.meta_tools import report_done
from chipchamp.tools.verif_tools import regress_run
from chipchamp.tools.verification_tools import verify_plan


@pytest.fixture
def design(tmp_path):
    root = tmp_path / "design"
    for directory in ("rtl", "tb", ".chipchamp"):
        (root / directory).mkdir(parents=True)
    (root / "rtl/leaf.sv").write_text("module leaf(output wire q); assign q=1'b0; endmodule\n")
    (root / "rtl/wrapper.sv").write_text("module wrapper(output wire q); leaf u_leaf(.q(q)); endmodule\n")
    (root / "rtl/other.sv").write_text("module other; endmodule\n")
    (root / "tb/tb_focus.sv").write_text('''module tb_focus;
wire q; wrapper dut(.q(q));
initial begin #1;
if (q !== 0) $display("CHIPCHAMP_FAIL: wrong q");
else $display("CHIPCHAMP_PASS: q checked");
$finish; end
endmodule
''')
    (root / "tb/tb_other.sv").write_text('''module tb_other;
other dut(); initial begin $display("CHIPCHAMP_PASS: other"); $finish; end
endmodule
''')
    (root / ".chipchamp/config.toml").write_text('''[project]
default_target = "chip"
[targets.chip]
top = "wrapper"
globs = ["rtl/*.sv"]
[tools]
sim_inner = "icarus"
[budgets.default]
v3_submissions_per_task = 20
''')
    manifest = {"defaults": {"seeds": [1, 2]}, "tests": [
        {"name": "focused", "target": "chip", "runner": {
            "kind": "sv", "tb_top": "tb_focus", "files": ["tb/tb_focus.sv"]}},
        {"name": "unrelated", "target": "chip", "runner": {
            "kind": "sv", "tb_top": "tb_other", "files": ["tb/tb_other.sv"]}},
        {"name": "foreign", "target": "different", "runner": {"tb_top": "absent"}}]}
    (root / ".chipchamp/tests.yaml").write_text(yaml.safe_dump(manifest))
    return ToolContext(Workspace(str(root)))


def test_hierarchy_selection_reaches_parents_without_name_matching(design):
    plan = verify_plan(design, changed_paths=["rtl/leaf.sv"])
    assert "error" not in plan, plan
    assert plan["selection"]["selected_tests"] == ["focused"]
    assert plan["selection"]["affected_modules"] == ["leaf", "tb_focus", "wrapper"]
    assert plan["selection"]["excluded_tests"] == ["unrelated"]
    assert "tb_focus" in plan["selection"]["reasons"]["focused"][0]
    assert "affected_regress" in plan["required_gates"]
    assert plan["test_jobs"] == 2 and plan["declared_suite_jobs"] == 4
    assert not design.task_jobs and not design.expected_sims and design.ledger.v3_used == 0
    assert all_tools()["verify.plan"].permission == "read"


@pytest.mark.parametrize("path", ["rtl/options.svh", ".chipchamp/tests.yaml", "rtl/package.sv"])
def test_unmapped_and_global_changes_keep_full_suite(design, path):
    if path != ".chipchamp/tests.yaml":
        (Path(design.ws.root) / path).write_text("// global configuration\n")
    plan = verify_plan(design, changed_paths=[path])
    assert plan["selection"]["selected_tests"] == ["focused", "unrelated"]
    assert plan["selection"]["mode"] == "full_suite" and plan["selection"]["warnings"]


def test_unresolved_test_is_retained(design):
    path = design.ws.dot / "tests.yaml"
    data = yaml.safe_load(path.read_text())
    data["tests"].append({"name": "unknown", "runner": {"tb_top": "missing_top"}})
    path.write_text(yaml.safe_dump(data))
    plan = verify_plan(design, changed_paths=["rtl/leaf.sv"])
    assert plan["selection"]["selected_tests"] == ["focused", "unknown"]
    assert "unresolved hierarchy" in plan["selection"]["reasons"]["unknown"][0]


def test_no_test_mapping_never_becomes_an_empty_success(design):
    (Path(design.ws.root) / "rtl/new.sv").write_text("module uncovered; endmodule")
    design = ToolContext(Workspace(design.ws.root))
    plan = verify_plan(design, changed_paths=["rtl/new.sv"])
    assert plan["selection"]["mode"] == "full_suite"
    assert any("No test maps" in w for w in plan["selection"]["warnings"])


def test_plan_fingerprint_and_budget_follow_actual_inputs(design):
    design.ledger.budget.v3_submissions = 1
    first = verify_plan(design, changed_paths=["rtl/leaf.sv"])
    assert first["budget"] == {"v3_remaining": 1, "v3_required": 2, "within_budget": False}
    assert verify_plan(design, changed_paths=["rtl/leaf.sv"])["plan_id"] == first["plan_id"]
    fs_edit(design, "rtl/leaf.sv", "1'b0", "1'b1")
    after = verify_plan(design)
    assert after["change_source"] == "session" and after["plan_id"] != first["plan_id"]


def test_plan_fingerprint_includes_testbenches_outside_target_sources(design):
    before = verify_plan(design, changed_paths=["rtl/leaf.sv"])
    tb = Path(design.ws.root) / "tb/tb_focus.sv"
    tb.write_text(tb.read_text().replace("#1", "#2"))
    after = verify_plan(design, changed_paths=["rtl/leaf.sv"])
    assert before["plan_id"] != after["plan_id"]


@pytest.mark.parametrize("kwargs", [
    {"changed_paths": ["../outside.sv"]}, {"changed_paths": ["/etc/passwd"]},
    {"changed_paths": ["rtl/missing.sv"]}, {"changed_paths": ["rtl"]},
    {"changed_paths": ["rtl/leaf.sv"], "seeds": []},
    {"changed_paths": ["rtl/leaf.sv"], "seeds": [True]},
    {"changed_paths": ["rtl/leaf.sv"], "base": "--help"},
])
def test_invalid_plans_are_refused_before_running_jobs(design, kwargs):
    assert "error" in verify_plan(design, **kwargs)
    assert not design.task_jobs


def test_denied_symlink_cannot_enter_plan(design):
    root = Path(design.ws.root)
    (root / "pdk").mkdir()
    (root / "pdk/secret.sv").write_text("module secret; endmodule")
    (root / "rtl/alias.sv").symlink_to(root / "pdk/secret.sv")
    assert "error" in verify_plan(design, changed_paths=["rtl/alias.sv"])


def test_git_changes_handle_staged_deleted_untracked_and_nested_workspace(design):
    root = Path(design.ws.root)
    repo = root.parent
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])
    git("init", "-q")
    git("add", "design")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "initial")
    (root / "rtl/leaf.sv").unlink()
    (root / "rtl/with space.sv").write_text("module spaced; endmodule")
    git("add", "design/rtl/with space.sv")
    (root / "rtl/untracked.sv").write_text("module untracked; endmodule")
    plan = verify_plan(design)
    assert set(plan["changed_files"]) == {"rtl/leaf.sv", "rtl/with space.sv", "rtl/untracked.sv"}
    assert plan["selection"]["mode"] == "full_suite"


def test_junit_preserves_each_case_and_distinguishes_launch_errors():
    def run(name, seed):
        if name == "launch":
            raise RuntimeError("license unavailable")
        return JobRecord("J-" + name, "sim", "fake", status=name, start_ts=1, end_ts=2,
                         summary="a < b & c\x1b\x00", seed=seed)
    result = RegressionManager(run).run(["passed", "failed", "timeout", "launch"], [1, 2], "reg<&")
    xml = ET.fromstring(junit_xml(result, "chip"))
    assert xml.attrib["tests"] == "8" and xml.attrib["failures"] == "4" and xml.attrib["errors"] == "2"
    assert len(xml.findall("testcase")) == 8
    assert xml.find("testcase/failure").attrib["message"].startswith("a < b & c")
    assert xml.find("testcase/error").attrib["message"] == "license unavailable"
    assert {n.attrib["name"] for n in xml.findall("testcase")} == {
        f"{name}[seed={seed}]" for name in ("passed", "failed", "timeout", "launch") for seed in (1, 2)}


class LocalAdapter(Adapter):
    name = "local-test"

    def version(self):
        return "1"

    def parse(self, plan, results):
        ok = all(r.rc == 0 for r in results)
        return ToolResult(ok=ok, kind="sim", adapter=self.name, status="pass" if ok else "fail")


@pytest.fixture
def fake_planner(monkeypatch):
    def plan(ctx, name, seed=1, **kwargs):
        return LocalAdapter(), Plan("sim", [Step(["true"], ctx.ws.root)], ctx.ws.root,
                                    "local-test", meta={"test_name": name}), ctx.target.sources, 10
    monkeypatch.setattr("chipchamp.tools.verif_tools._plan_for_test", plan)


def test_affected_run_reports_results_without_waiving_full_suite(design, fake_planner):
    fs_edit(design, "rtl/leaf.sv", "1'b0", "1'b1")
    result = regress_run(design, affected=True, seeds=[1], junit="work/ci.xml")
    assert result["passed"] == 1 and result["selected_tests"] == ["focused"]
    xml = ET.parse(Path(design.ws.root) / result["reports"]["junit"]).getroot()
    assert xml.attrib["tests"] == "1"
    report = json.loads((Path(design.ws.root) / result["reports"]["json"]).read_text())
    assert report["cases"][0]["test"] == "focused" and report["cases"][0]["job"]["status"] == "passed"
    assert report["plan"]["plan_id"] == result["plan"]["plan_id"]
    done = report_done(design)
    regression_gate = next(g for g in done["gate_table"] if g["name"] == "affected_regress")
    assert regression_gate["status"] == "missing" and "unrelated" in regression_gate["detail"]
    assert "foreign" not in regression_gate["detail"]


def test_budget_blocked_tests_still_appear_as_junit_errors(design, fake_planner):
    design.ledger.budget.v3_submissions = 1
    result = regress_run(design, seeds=[1])
    assert result["total"] == 2 and result["passed"] == 1 and result["errored"] == 1
    xml = ET.parse(Path(design.ws.root) / result["reports"]["junit"]).getroot()
    assert xml.attrib["tests"] == "2" and xml.attrib["errors"] == "1"
    assert "budget" in xml.find("testcase/error").attrib["message"]


@pytest.mark.parametrize("kwargs", [
    {"affected": True, "tag": "smoke"}, {"affected": True, "tests": ["focused"]},
    {"changed_paths": ["rtl/leaf.sv"]}, {"junit": "../escape.xml"},
    {"junit": "rtl/leaf.sv"}, {"tests": ["foreign"]}, {"seeds": []},
])
def test_invalid_regression_request_does_not_launch(design, kwargs):
    assert "error" in regress_run(design, **kwargs)
    assert not design.task_jobs


def test_cli_plan_json_and_bad_seed_exit_codes(design):
    runner = CliRunner()
    args = ["--root", design.ws.root, "--json", "verify-plan", "--changed", "rtl/leaf.sv"]
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["selection"]["selected_tests"] == ["focused"]
    assert runner.invoke(cli, args + ["--seeds", "1,no"]).exit_code == 2
    assert runner.invoke(cli, args + ["--base", "nonexistent-ref"]).exit_code == 1


def test_cli_failed_regression_is_nonzero_even_in_json_mode(design, fake_planner, monkeypatch):
    design.ledger.budget.v3_submissions = 0
    monkeypatch.setattr("chipchamp.cli.verify._ctx", lambda *args: design)
    result = CliRunner().invoke(cli, ["--root", design.ws.root, "--json", "regress", "--seeds", "1"])
    assert result.exit_code == 1
    assert json.loads(result.output)["errored"] == 2


def test_live_icarus_targeted_run_and_failure_export(design):
    from conftest import has
    if not has("iverilog"):
        pytest.skip("iverilog not installed")
    result = regress_run(design, affected=True, changed_paths=["rtl/leaf.sv"], seeds=[1])
    assert result["passed"] == 1 and result["failed"] == result["errored"] == 0, result
    fs_edit(design, "rtl/leaf.sv", "1'b0", "1'b1")
    failure = regress_run(design, affected=True, seeds=[1], junit="work/failure.xml")
    assert failure["failed"] == 1 and failure["passed"] == 0, failure
    xml = ET.parse(Path(design.ws.root) / failure["reports"]["junit"]).getroot()
    assert "wrong q" in xml.find("testcase/failure").text
