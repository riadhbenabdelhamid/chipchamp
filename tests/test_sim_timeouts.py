"""A simulation's time limit belongs to the engineer, not to a constant:
tests.yaml and config set it, the agent may raise it within a ceiling, a
silent simulator is told apart from a slow one, and a timeout says what to
do about it."""
from __future__ import annotations

import time
from types import SimpleNamespace as NS

from chipchamp.adapters.base import Plan, Step
from chipchamp.jobs.record import JobRecord, StepLog
from chipchamp.jobs.runner import JobRunner
from chipchamp.tools.verif_tools import (TIMEOUT_CEILING_S, _finish_plan,
                                         _idle_for, _timeout_for,
                                         _timeout_report)


def _ctx(config=None, defaults=None):
    return NS(ws=NS(config=config or {}, test_defaults=defaults or {}))


def test_precedence_builtin_config_defaults_test_argument():
    t = {"name": "fifo_smoke"}
    assert _timeout_for(_ctx(), t, 120) == (120.0, "built-in default")
    c = _ctx({"tools": {"sim_timeout_s": 240}})
    assert _timeout_for(c, t, 120) == (240.0, "config [tools] sim_timeout_s")
    c = _ctx({"tools": {"sim_timeout_s": 240}}, {"timeout_s": 60})
    assert _timeout_for(c, t, 120) == (60.0, "tests.yaml defaults")
    t2 = {"name": "fifo_long", "timeout_s": 900}
    assert _timeout_for(c, t2, 120) == (900.0, "tests.yaml test 'fifo_long'")
    assert _timeout_for(c, t2, 120, override=1800) == (1800.0, "timeout_s argument")


def test_the_agent_cannot_exceed_the_ceiling():
    secs, why = _timeout_for(_ctx(), {"name": "x"}, 120, override=10 ** 9)
    assert secs == TIMEOUT_CEILING_S and "capped" in why
    c = _ctx({"tools": {"max_timeout_s": 600}})
    assert _timeout_for(c, None, 120, override=5000)[0] == 600.0


def test_idle_timeout_is_off_unless_someone_sets_it():
    assert _idle_for(_ctx(), {"name": "x"}) is None
    assert _idle_for(_ctx({"tools": {"idle_timeout_s": 90}}), None) == 90.0
    assert _idle_for(_ctx(defaults={"idle_timeout_s": 30}), None) == 30.0
    assert _idle_for(_ctx(), {"name": "x", "idle_timeout_s": 15}) == 15.0


def test_example_workspace_defaults_are_read(ctx):
    assert ctx.ws.test_defaults.get("timeout_s") == 60     # documented since day one
    plan = Plan(kind="sim", adapter="x", workdir=".", steps=[])
    _, p, _, secs = _finish_plan(ctx, ctx.find_test("fifo_smoke"), None, plan, [], 120)
    assert secs == 60.0 and p.meta["timeout_source"] == "tests.yaml defaults"


def test_runner_tells_a_hang_from_a_long_run(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    job = tmp_path / "runs" / "J-I"
    job.mkdir(parents=True)
    t0 = time.time()
    sr = r._run_step(Step(argv=["bash", "-c", "echo hi; sleep 10"], cwd=str(tmp_path)),
                     job, 0, timeout=30, idle_timeout=0.7)
    assert sr.timed_out and sr.timeout_reason == "idle" and time.time() - t0 < 5
    assert "no output for 0.7s" in sr.stderr
    sr = r._run_step(Step(argv=["bash", "-c", "while true; do echo tick; sleep 0.2; done"],
                          cwd=str(tmp_path)), job, 1, timeout=1.0, idle_timeout=5)
    assert sr.timed_out and sr.timeout_reason == "wall"   # kept talking, still too long
    assert "after 1s" in sr.stderr


def test_timeout_report_names_rule_and_remedy():
    plan = Plan(kind="sim", adapter="x", workdir=".", steps=[],
                meta={"timeout_s": 300.0, "timeout_source": "built-in default",
                      "idle_timeout_s": 60.0})
    rec = JobRecord(id="J-1", kind="sim", adapter="x", status="timeout",
                    steps=[StepLog(argv=[], rc=124, duration_s=300, log_path="",
                                   timed_out=True, timeout_reason="wall")])
    tr = _timeout_report(rec, plan)
    assert tr["after_s"] == 300.0 and tr["reason"] == "wall"
    assert "timeout_s in tests.yaml" in tr["hint"] and "detach=true" in tr["hint"]
    rec.steps[0].timeout_reason = "idle"
    tr = _timeout_report(rec, plan)
    assert tr["after_s"] == 60.0 and "no output" in tr["hint"]
    rec.status = "passed"
    assert _timeout_report(rec, plan) is None
