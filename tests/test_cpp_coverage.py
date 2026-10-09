"""Collect actual RTL coverage from native harnesses without changing verdicts."""
from pathlib import Path

import pytest

from chipchamp.adapters.verilator import VerilatorAdapter
from chipchamp.coverage.history import CoverageHistory
from chipchamp.policy.diff import Diff, FileChange
from chipchamp.tools.verif_tools import cov_run, cov_history, cov_compare, regress_run
from test_runtime_resume import configure
from test_verification_planning import design  # noqa: F401


RTL = """module wrapper(input wire a, output reg q);
always_comb begin
  if (a) q = 1'b1;
  else q = 1'b0;
end
endmodule
"""


def harness(ctx, guarded=True, failing=False, argc=True):
    root = Path(ctx.ws.root)
    (root / "rtl/wrapper.sv").write_text(RTL)
    # An explicit context catches exporters that incorrectly assume the default
    # global context; the guard must run before either the DUT or context dies.
    code = '''#include "Vwrapper.h"
#include "chipchamp_coverage.hpp"
#include <cstdio>
int main(@ARGS@) {
    VerilatedContext context;
    @COMMAND_ARGS@
    Vwrapper dut{&context};
    @GUARD@
    dut.a = 0; dut.eval();
    if (dut.q != 0) return 1;
    dut.a = 1; dut.eval();
    if (dut.q != 1) return 2;
    std::puts("CHIPCHAMP_PASS: both branches checked");
    return @RESULT@;
}
'''.replace("@ARGS@", "int argc, char** argv" if argc else "")
    code = code.replace("@COMMAND_ARGS@", "context.commandArgs(argc, argv);" if argc else "")
    code = code.replace("@GUARD@", "chipchamp::Coverage coverage{&context};" if guarded else "")
    code = code.replace("@RESULT@", "3" if failing else "0")
    (root / "tb/main.cpp").write_text(code)
    return configure(ctx, {"kind": "verilator_cpp", "top": "wrapper", "cpp": ["tb/main.cpp"]})


@pytest.fixture
def cpp_workspace(design):
    if not VerilatorAdapter().available():
        pytest.skip("Verilator unavailable")
    return design


def test_cpp_coverage_history_line_mapping_and_regular_resume(cpp_workspace):
    ctx = harness(cpp_workspace)
    first = cov_run(ctx, "runtime", seed=7)
    assert "error" not in first, first
    assert first["observation"]["summary"]["bins"] > 0
    assert first["observation"]["summary"]["total"] > 0
    assert ctx.runner.get(first["job"]).input_snapshot["complete"]
    second = cov_run(ctx, "runtime", seed=7)
    assert "error" not in second and second["delta"]["delta_percent"] == 0, second
    assert cov_history(ctx, "runtime", 7)["total"] == 2
    assert cov_compare(ctx, first["job"], second["job"])["baseline_check"]["ok"]
    # The same included guard is a no-op in ordinary, non-instrumented runs.
    run = regress_run(ctx, tests=["runtime"], seeds=[7])
    assert run["passed"] == 1, run
    assert regress_run(ctx, resume=run["checkpoint"])["resumed"] == 1
    edited = RTL.replace("if (a)", "if (!a)")
    (Path(ctx.ws.root) / "rtl/wrapper.sv").write_text(edited)
    order = CoverageHistory(ctx).order(["runtime"], Diff([
        FileChange("rtl/wrapper.sv", "modified", RTL, edited)]))
    assert order["method"] == "coverage_lines", order
    assert order["impact"]["regions"][0]["evidence"]


def test_cpp_no_export_and_failure_cannot_reuse_old_coverage(cpp_workspace):
    ctx = harness(cpp_workspace)
    good = cov_run(ctx, "runtime")
    assert "error" not in good, good
    ctx = harness(ctx, guarded=False)
    missing = cov_run(ctx, "runtime")
    assert "chipchamp::Coverage" in missing["error"], missing
    assert cov_history(ctx)["total"] == 1
    ctx = harness(ctx, failing=True)
    failed = cov_run(ctx, "runtime")
    assert "error" in failed, failed
    assert cov_history(ctx)["total"] == 1


def test_cpp_guard_does_not_require_command_args(cpp_workspace):
    ctx = harness(cpp_workspace, argc=False)
    result = cov_run(ctx, "runtime", seed=19)
    assert "error" not in result and result["observation"]["seed"] == 19, result


def test_multiple_guards_cannot_overwrite_coverage_silently(cpp_workspace):
    ctx = harness(cpp_workspace)
    source = Path(ctx.ws.root) / "tb/main.cpp"
    source.write_text(source.read_text().replace("dut.a = 0;", "chipchamp::Coverage second{&context};\n    dut.a = 0;"))
    result = cov_run(ctx, "runtime")
    assert "error" in result and cov_history(ctx)["total"] == 0, result
