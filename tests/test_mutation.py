"""Mutation experiments must distinguish checked bugs from invalid experiments."""
from pathlib import Path
import json
import subprocess
import sys
import time

import pytest
import yaml
from click.testing import CliRunner

from chipchamp.adapters.base import Adapter, Plan, Step, ToolResult
from chipchamp.bench.mutators import generate_mutants
from chipchamp.bench.runner import MutationRunner
from chipchamp.cli import cli
from chipchamp.config import Workspace
from chipchamp.tools import all_tools
from chipchamp.tools.context import ToolContext
from chipchamp.tools.verification_tools import mutation_run
from chipchamp.util.hashing import sha256_file
from conftest import requires_icarus, requires_verilator


SOURCE = """module dut(input wire [3:0] count, output wire [3:0] q);
assign q = count + 1;
endmodule
"""


@pytest.fixture
def design(tmp_path):
    root = tmp_path / "design"
    for folder in ("rtl", "tb", ".chipchamp"):
        (root / folder).mkdir(parents=True)
    (root / "rtl/dut.sv").write_text(SOURCE)
    (root / "tb/tb_check.sv").write_text('''module tb_check;
reg [3:0] count=3; wire [3:0] q;
dut d(count,q);
initial begin #1;
if (q !== 4) $display("CHIPCHAMP_FAIL: increment check");
else $display("CHIPCHAMP_PASS");
$finish; end
endmodule
''')
    (root / "tb/tb_weak.sv").write_text('''module tb_weak;
wire [3:0] q; dut d(4'd3,q);
initial begin #1; $display("CHIPCHAMP_PASS"); $finish; end
endmodule
''')
    (root / ".chipchamp/config.toml").write_text('''[project]
default_target = "chip"
[targets.chip]
top = "dut"
globs = ["rtl/*.sv"]
[tools]
sim_inner = "icarus"
[budgets.default]
v3_submissions_per_task = 30
''')
    (root / ".chipchamp/tests.yaml").write_text(yaml.safe_dump({"defaults": {"seeds": [1, 2]}, "tests": [
        {"name": name, "runner": {"tb_top": "tb_" + name, "files": [f"tb/tb_{name}.sv"]}}
        for name in ("check", "weak")]}))
    ctx = ToolContext(Workspace(str(root)))
    ctx.edits["rtl/dut.sv"] = {"old": SOURCE.replace("count + 1", "1 + count"),
                               "new": SOURCE, "status": "modified"}
    return ctx


def test_operators_ignore_non_code_and_filter_before_site_cap():
    text = '''// count + 1; a == b
/* a == b */ string s="count + 1";
assign a = (b === c);
assign a = count + 1;
assign b = count + 1;
assign c = count + 1;
'''
    mutants = generate_mutants(text, "dut.sv", per_op=1, lines={6})
    assert len(mutants) == 1 and mutants[0].line == 6
    assert mutants[0].mutated.splitlines()[5] == "assign c = count - 1;"
    all_sites = generate_mutants(text, "dut.sv", per_op=10)
    assert all(m.line >= 4 for m in all_sites)
    assert len(all_sites) == 3


@requires_icarus
def test_live_detection_baseline_seeds_and_isolated_evidence(design):
    original = sha256_file(Path(design.ws.root) / "rtl/dut.sv")
    result = mutation_run(design)
    assert result["status"] == "completed", result
    assert len(result["baseline"]) == 4 and all(c["status"] == "passed" for c in result["baseline"])
    assert result["counts"] == {"detected": 1}
    assert result["detection_rate"] == 1 and result["jobs_submitted"] == 5
    m = result["mutants"][0]
    assert m["line"] == 2 and m["detected_by"] == {"test": "check", "seed": 1}
    assert (Path(design.ws.root) / m["patch"]).read_text().count("count - 1") == 1
    for c in result["baseline"] + m["cases"]:
        record = json.loads((Path(design.ws.root) / c["record"]).read_text())
        assert record["target_name"] == "chip" and record["seed"] == c["seed"]
        assert record["evidence_hashes"]
    assert sha256_file(Path(design.ws.root) / "rtl/dut.sv") == original
    assert not design.task_jobs and not design.expected_sims
    assert not design.runner.list_jobs() and design.ledger.v3_used == 5
    assert design.ledger.cpu_seconds > 0
    saved = json.loads((Path(design.ws.root) / result["reports"]["json"]).read_text())
    assert saved == result
    assert "increment becomes decrement" in (Path(design.ws.root) / result["reports"]["markdown"]).read_text()


@requires_icarus
def test_survivor_requires_whole_matrix_and_baseline_failure_stops(design):
    result = mutation_run(design, tests=["weak"])
    assert result["counts"] == {"survived": 1}
    assert len(result["mutants"][0]["cases"]) == 2 and result["detection_rate"] == 0
    tb = Path(design.ws.root) / "tb/tb_check.sv"
    tb.write_text(tb.read_text().replace("q !== 4", "q !== 5"))
    result = mutation_run(design, tests=["check"])
    assert result["status"] == "baseline_failed"
    assert result["jobs_submitted"] == 1 and result["baseline"][0]["status"] == "failed"
    assert result["counts"] == {"not_run": 1} and result["detection_rate"] is None


@pytest.mark.parametrize("kind", ["compile_error", "timeout", "crash", "false_pass"])
@requires_icarus
def test_inconclusive_mutants_never_count_as_detected(design, monkeypatch, kind):
    from chipchamp.tools import verif_tools
    real = verif_tools._plan_for_test

    def plan(ctx, test, **kwargs):
        got = real(ctx, test, **kwargs)
        mutant = "count - 1" in (Path(ctx.ws.root) / "rtl/dut.sv").read_text()
        if mutant:
            if kind == "compile_error":
                (Path(ctx.ws.root) / "rtl/dut.sv").write_text("invalid syntax")
            else:
                body = {"timeout": "import time; time.sleep(5)",
                        "crash": "raise SystemExit(1)",
                        "false_pass": "print('CHIPCHAMP_PASS'); raise SystemExit(1)"}[kind]
                got[1].steps[-1].argv = [sys.executable, "-c", body]
        return got

    monkeypatch.setattr(verif_tools, "_plan_for_test", plan)
    result = mutation_run(design, tests=["check"], seeds=[1], timeout_s=0.3)
    expected = kind if kind in ("compile_error", "timeout") else "error"
    assert result["counts"] == {expected: 1}, result
    assert result["detection_rate"] is None


@requires_icarus
def test_budget_preserves_pending_matrix_and_charges_parent(design):
    result = mutation_run(design, tests=["weak"], max_jobs=3)
    assert result["status"] == "budget_exhausted" and result["jobs_submitted"] == 3
    assert result["mutants"][0]["status"] == "incomplete"
    assert result["detection_rate"] is None
    assert result["matrix"] == [{"test": "weak", "seed": 1}, {"test": "weak", "seed": 2}]
    design.ledger.budget.v3_submissions = design.ledger.v3_used
    again = mutation_run(design)
    assert again["status"] == "budget_exhausted" and again["jobs_submitted"] == 0


@requires_icarus
def test_coverage_history_orders_tests_without_removing_cases(design, monkeypatch):
    from chipchamp.coverage.history import CoverageHistory
    real = CoverageHistory.order

    def order(self, names, changes):
        result = real(self, names, changes)
        result["tests"] = ["weak", "check"]
        result["method"] = "coverage_lines"
        return result

    monkeypatch.setattr(CoverageHistory, "order", order)
    result = mutation_run(design, seeds=[1])
    assert [c["test"] for c in result["baseline"]] == ["weak", "check"]
    assert [c["status"] for c in result["mutants"][0]["cases"]] == ["passed", "detected"]


@pytest.mark.parametrize("kwargs", [{"max_jobs": 0}, {"timeout_s": float("nan")},
    {"max_seconds": float("inf")}, {"max_mutants": True}, {"seeds": []},
    {"tests": ["missing"]}, {"changed_paths": ["../escape"]}])
def test_invalid_arguments_submit_nothing(design, kwargs):
    assert "error" in mutation_run(design, **kwargs)
    assert design.ledger.v3_used == 0
    assert all_tools()["mutation.run"].permission == "submit"


def test_no_supported_sites_and_protected_files_are_explicit(design):
    design.policy.protected_paths.append("rtl/*.sv")
    result = mutation_run(design)
    assert result["status"] == "no_mutations" and "protected" in result["skipped"][0]["reason"]
    assert result["detection_rate"] is None and result["jobs_submitted"] == 0


@requires_icarus
def test_snapshot_omits_links_and_denied_data_and_refuses_absolute_sources(design, tmp_path):
    root = Path(design.ws.root)
    (root / "secret.lic").write_text("secret")
    (root / "alias").symlink_to(tmp_path)
    result = mutation_run(design, tests=["check"], seeds=[1])
    assert result["status"] == "completed", result
    snapshot_dir = root / Path(result["reports"]["json"]).parent / "snapshot"
    assert not (snapshot_dir / "alias").exists() and not (snapshot_dir / "secret.lic").exists()
    config = root / ".chipchamp/config.toml"
    config.write_text(config.read_text().replace('globs = ["rtl/*.sv"]', f'globs = ["{root}/rtl/*.sv"]'))
    design = ToolContext(Workspace(str(root)))
    result = mutation_run(design, changed_paths=["rtl/dut.sv"], tests=["check"], seeds=[1])
    assert result["status"] == "error" and result["jobs_submitted"] == 0
    assert "workspace-local" in result["reason"]


def test_cumulative_deadline_covers_multiple_steps(design, tmp_path):
    class Local(Adapter):
        name = "test"
        def version(self):
            return "1"
        def parse(self, plan, results):
            return ToolResult(True, "sim", self.name, status="pass")
    runner = MutationRunner(tmp_path / "jobs", design.runner)
    runner.deadline = time.monotonic() + 0.5
    plan = Plan("sim", [Step([sys.executable, "-c", "import time; time.sleep(.35)"], str(tmp_path))
                        for _ in range(3)], str(tmp_path), "test")
    started = time.monotonic()
    record, _ = runner.submit(plan, Local(), timeout=10, no_cache=True)
    assert record.status == "timeout" and len(record.steps) < 3
    assert time.monotonic() - started < 2


@requires_icarus
def test_cli_changed_line_scope_reports_and_exit_codes(design):
    root = Path(design.ws.root)
    for args in (["init", "-q"], ["add", "rtl", "tb", ".chipchamp"],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "rtl/dut.sv").write_text(SOURCE.replace("count + 1", "count + 1 /* changed */"))
    args = ["--root", str(root), "--json", "mutate", "--changed", "rtl/dut.sv", "--seeds", "1"]
    result = CliRunner().invoke(cli, args + ["--tests", "check"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["counts"] == {"detected": 1} and data["mutants"][0]["line"] == 2
    weak = CliRunner().invoke(cli, args + ["--tests", "weak"])
    assert weak.exit_code == 1 and json.loads(weak.output)["counts"] == {"survived": 1}
    # Old workspaces need not update .gitignore before using the feature:
    # mutation sources and binaries never become a subsequent change scope.
    from chipchamp.verification import _changes
    # Use Git discovery instead of this fixture's session edit list.
    design.edits.clear()
    diff, _, _ = _changes(design, None, "HEAD")
    assert [c.path for c in diff.files] == ["rtl/dut.sv"]


@requires_icarus
def test_snapshot_preserves_local_configuration_and_line_endings(design):
    root = Path(design.ws.root)
    (root / "rtl/dut.sv").write_bytes(SOURCE.replace("\n", "\r\n").encode())
    (root / ".chipchamp/config.local.toml").write_text('[targets.chip.defines]\nLOCAL_OVERRIDE = "1"\n')
    design = ToolContext(Workspace(str(root)))
    result = mutation_run(design, changed_paths=["rtl/dut.sv"], tests=["check"], seeds=[1])
    assert result["counts"] == {"detected": 1}, result
    case = result["mutants"][0]["cases"][0]
    record = json.loads((root / case["record"]).read_text())
    assert "-DLOCAL_OVERRIDE=1" in record["steps"][0]["argv"]
    mutated = root / case["workspace"] / "rtl/dut.sv"
    assert mutated.read_bytes() == SOURCE.replace("+", "-").replace("\n", "\r\n").encode()
    assert result["mutants"][0]["source_sha256"] == sha256_file(root / "rtl/dut.sv")


@requires_verilator
def test_native_cpp_dispatch_and_build_phases(design):
    root = Path(design.ws.root)
    (root / "tb/main.cpp").write_text('''#include "Vdut.h"
#include <cstdio>
int main() {
    Vdut dut;
    dut.count = 3; dut.eval();
    if (dut.q != 4) { std::puts("CHIPCHAMP_FAIL: wrong increment"); return 1; }
    std::puts("CHIPCHAMP_PASS"); return 0;
}
''')
    (root / ".chipchamp/tests.yaml").write_text(yaml.safe_dump({"tests": [{
        "name": "cpp", "runner": {"kind": "verilator_cpp", "top": "dut", "cpp": ["tb/main.cpp"]}}]}))
    result = mutation_run(design, tests=["cpp"], seeds=[7], max_jobs=2)
    assert result["status"] == "completed" and result["counts"] == {"detected": 1}, result
    case = result["mutants"][0]["cases"][0]
    record = json.loads((root / case["record"]).read_text())
    assert len(record["steps"]) == 3 and record["adapter"] == "verilator"


@requires_icarus
def test_cocotb_assertions_detect_mutants(design):
    from chipchamp.adapters.cocotb_adapter import CocotbAdapter
    if not CocotbAdapter().available():
        pytest.skip("cocotb unavailable")
    root = Path(design.ws.root)
    (root / "tb/test_mutant.py").write_text('''import cocotb
from cocotb.triggers import Timer
@cocotb.test()
async def checks(dut):
    dut.count.value = 3
    await Timer(1, unit="ns")
    assert int(dut.q.value) == 4
''')
    (root / ".chipchamp/tests.yaml").write_text(yaml.safe_dump({"tests": [{
        "name": "python", "runner": {"kind": "cocotb", "module": "test_mutant", "toplevel": "dut"}}]}))
    result = mutation_run(design, tests=["python"], seeds=[9])
    assert result["status"] == "completed" and result["counts"] == {"detected": 1}, result
