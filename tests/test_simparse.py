"""One compile-then-run verdict for every simulator adapter — and the drift
it removes: two adapters glued stdout to stderr with no newline, so a UVM
tally at the start of stderr read as a FATAL event."""
from __future__ import annotations

import pytest

from chipchamp.adapters.base import Plan, Step, StepResult
from chipchamp.adapters.icarus import IcarusAdapter
from chipchamp.adapters.questa import QuestaAdapter
from chipchamp.adapters.simparse import parse_compile_run_sim, run_log, status_from_log
from chipchamp.adapters.vcs import VcsAdapter
from chipchamp.adapters.verilator import VerilatorAdapter
from chipchamp.brand import marker

ADAPTERS = [IcarusAdapter, VcsAdapter, QuestaAdapter, VerilatorAdapter]


def _plan(seed=7):
    return Plan(kind="sim", adapter="x", workdir=".", steps=[Step(argv=["a"], cwd="."),
                                                             Step(argv=["b"], cwd=".")],
                meta={"seed": seed})


def _step(rc=0, out="", err="", timed_out=False):
    return StepResult(argv=["x"], rc=rc, stdout=out, stderr=err, timed_out=timed_out)


@pytest.mark.parametrize("cls", ADAPTERS)
def test_all_four_agree_on_the_common_cases(cls):
    ad = cls()
    ok = ad.parse(_plan(), [_step(), _step(out=f"sim done\n{marker('PASS')}\n")])
    assert ok.ok and ok.status == "pass" and ok.metrics["seed"] == 7
    fail = ad.parse(_plan(), [_step(), _step(out=f"{marker('FAIL')}: mismatch at 120ns\n")])
    assert not fail.ok and fail.status == "fail" and "mismatch" in fail.summary
    cf = ad.parse(_plan(), [_step(rc=1, err="syntax error")])
    assert cf.status == "error" and cf.metrics.get("phase") == "compile"
    nr = ad.parse(_plan(), [_step()])
    assert nr.status == "error" and "did not run" in nr.summary.lower() or "run" in nr.summary
    assert ad.parse(_plan(), []).status == "error"          # was an IndexError
    to = ad.parse(_plan(), [_step(), _step(timed_out=True)])
    assert to.status == "timeout"


@pytest.mark.parametrize("cls", [VcsAdapter, QuestaAdapter])
def test_a_uvm_tally_at_the_start_of_stderr_is_not_a_fatal_event(cls):
    """stdout ends without a newline; stderr begins with the report summary.
    Joined without a newline, "sim doneUVM_FATAL :    0" escaped the
    line-anchored tally filter and matched the FATAL keyword."""
    out = f"{marker('PASS')}\nsim done"
    err = "UVM_FATAL :    0\nUVM_ERROR :    0\n"
    res = cls().parse(_plan(), [_step(), _step(out=out, err=err)])
    assert res.status == "pass", res.summary


def test_run_log_always_separates_the_streams():
    assert run_log(_step(out="a", err="b")) == "a\nb"
    assert status_from_log("x\nUVM_FATAL :    0\n" + marker("PASS"), _step())[0] == "pass"


def test_missing_artifacts_are_dropped_for_every_adapter(tmp_path):
    present = tmp_path / "waves.vcd"
    present.write_text("")
    plan = _plan()
    plan.artifacts = {"waves": str(present), "log": str(tmp_path / "nope.log")}
    for cls in ADAPTERS:
        res = cls().parse(plan, [_step(), _step(out=marker("PASS"))])
        assert res.artifacts == {"waves": str(present)}, cls.__name__


def test_runtime_diagnostics_are_opt_in_per_adapter():
    log = "tb.sv:42: assertion failed: q mismatch\n" + marker("FAIL")
    with_ = parse_compile_run_sim("a", _plan(), [_step(), _step(out=log)], [], runtime_tool="iverilog")
    without = parse_compile_run_sim("a", _plan(), [_step(), _step(out=log)], [])
    assert with_.metrics["assertion_failures"] == 1 and with_.diagnostics[0].line == 42
    assert "assertion_failures" not in without.metrics and without.diagnostics == []


def test_old_private_names_still_resolve():
    from chipchamp.adapters.icarus import _runtime_diags, _status_from_log
    assert _status_from_log(marker("PASS"), _step())[0] == "pass"
    assert _runtime_diags("a.sv:3: ERROR x")[0].tool == "iverilog"
