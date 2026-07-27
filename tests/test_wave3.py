"""Wave 3 — the ceiling: a bounded working set, and jobs that detach.

These are one project seen from two ends. A task that runs for hours needs a
context that survives hours, and a job that finishes at 3am needs something to
pick the work back up.
"""
from __future__ import annotations

import json
import time

import pytest

from chipchamp.adapters.base import Adapter, Plan, Step, ToolResult
from chipchamp.agent import working_set
from chipchamp.agent.loop import transcript_size
from chipchamp.jobs.runner import JobRunner


# ---- 1a: the working set ----------------------------------------------------

def _tool_turn(name, payload):
    return {"role": "tool",
            "content": [{"id": "1", "name": name, "output": json.dumps(payload)}]}


def _big_sim(i):
    return _tool_turn("sim.run", {
        "job": f"J-{i:04d}", "status": "failed", "sim_status": "fail",
        "summary": "3 assertion failures",
        "errors": [{"message": "x" * 300, "file": "fifo.sv", "line": 31}
                   for _ in range(6)],
        "log_excerpt": "y" * 3000})


def test_a_digest_keeps_the_verdict_and_the_address():
    out, changed = working_set.digest_output(
        "sim.run", json.dumps({"job": "J-0342", "status": "failed",
                               "summary": "3 assertion failures",
                               "waveform": "z" * 5000}))
    assert changed
    kept = json.loads(out.split(" [evicted:")[0])
    assert kept["job"] == "J-0342" and kept["status"] == "failed"
    assert "waveform" in out          # named as evicted…
    assert "z" * 100 not in out       # …but its body is gone
    assert "job.log" in out           # and the way back is right there


def test_a_small_result_is_left_alone():
    """Rewriting everything would churn the prompt cache for nothing."""
    small = json.dumps({"job": "J-1", "status": "passed"})
    out, changed = working_set.digest_output("lint.run", small)
    assert out == small and changed is False


def test_non_json_output_keeps_its_head_and_says_what_went():
    out, changed = working_set.digest_output("job.log", "L" * 4000)
    assert changed and out.startswith("L" * 100)
    assert "evicted from context" in out and "job.log" in out


def test_a_result_without_a_job_points_at_the_file_or_the_tool():
    out, _ = working_set.digest_output(
        "fs.read", json.dumps({"path": "rtl/fifo.sv", "text": "q" * 2000}))
    assert "fs.read rtl/fifo.sv" in out
    out, _ = working_set.digest_output(
        "design.cone", json.dumps({"nodes": ["n" * 50] * 60}))
    assert "re-run design.cone" in out


def test_compaction_brings_a_transcript_under_budget():
    t = [{"role": "user", "content": "why does fifo_smoke fail?"}]
    for i in range(12):
        t.append({"role": "assistant", "content": "checking",
                  "tool_calls": [{"input": {"test": "fifo_smoke"}}]})
        t.append(_big_sim(i))
    before = transcript_size(t)["chars"]
    out, rep = working_set.compact(t, budget_chars=8000, keep_recent=1)
    assert before > 8000
    assert rep["compacted"] and rep["under_budget"] is True
    assert rep["after"] <= 8000 < before
    assert rep["freed"] > before / 2      # most of the weight was bodies


def test_the_recent_window_is_never_sacrificed_to_hit_a_number():
    """A few large recent results can leave the transcript over budget. That is
    the right outcome — blinding the model to what it just did costs more than
    the overrun — but it must be reported, not implied away."""
    t = [{"role": "user", "content": "go"}]
    for i in range(12):
        t.append(_big_sim(i))
    out, rep = working_set.compact(t, budget_chars=2000, keep_recent=6)
    assert rep["compacted"] and rep["freed"] > 0
    assert rep["under_budget"] is False        # honest about falling short
    assert rep["after"] > 2000


def test_compaction_never_evicts_what_has_no_backing_store():
    """User turns and assistant prose exist nowhere else — they are the only
    things in the transcript that cannot be re-fetched."""
    t = [{"role": "user", "content": "the FIFO overflows on seed 3, find it"}]
    for i in range(12):
        t.append({"role": "assistant", "content": f"reasoning step {i}"})
        t.append(_big_sim(i))
    out, rep = working_set.compact(t, budget_chars=4000)
    assert out[0]["content"] == "the FIFO overflows on seed 3, find it"
    for i in range(12):
        assert out[1 + 2 * i]["content"] == f"reasoning step {i}"


def test_compaction_spares_the_recent_window():
    """The model is mid-thought about the last few steps; evicting those would
    make it re-fetch what it just asked for."""
    t = [{"role": "user", "content": "go"}]
    for i in range(12):
        t.append(_big_sim(i))
    out, _ = working_set.compact(t, budget_chars=3000, keep_recent=4)
    assert len(json.loads(out[-1]["content"][0]["output"])["errors"]) == 6
    assert "evicted" in out[1]["content"][0]["output"]


def test_an_evicted_result_still_carries_its_job_id():
    """The address is the whole point — it must survive in the transcript, not
    in a convention the model has to remember."""
    t = [{"role": "user", "content": "go"}] + [_big_sim(i) for i in range(12)]
    out, _ = working_set.compact(t, budget_chars=2000, keep_recent=1)
    assert "J-0000" in out[1]["content"][0]["output"]


def test_a_transcript_under_budget_is_returned_untouched():
    t = [{"role": "user", "content": "hi"}, _tool_turn("lint.run", {"errors": 0})]
    out, rep = working_set.compact(t, budget_chars=100_000)
    assert out is t and rep["compacted"] is False


def test_budget_follows_the_models_own_window():
    gw = type("G", (), {"options": {"num_ctx": 32768}})()
    small = working_set.budget_for(gw, {})
    big = working_set.budget_for(type("G", (), {"options": {"num_ctx": 131072}})(), {})
    assert 8000 < small < big
    # explicit config wins, and 0 means "never evict"
    assert working_set.budget_for(gw, {"model": {"context_chars": 12345}}) == 12345
    assert working_set.budget_for(gw, {"model": {"context_chars": 0}}) == 0
    # no signal at all → sized for a small local model, not an optimistic guess
    assert working_set.budget_for(type("G", (), {})(), {}) == 60_000


# ---- #2: detached jobs ------------------------------------------------------

class _SlowAdapter(Adapter):
    name = "slow"
    kinds = ("sim",)

    def version(self):
        return "1.0"

    def available(self):
        return True

    def plan(self, **kw):
        raise NotImplementedError

    def parse(self, plan, results):
        return ToolResult(ok=True, kind="sim", adapter=self.name,
                          status="pass", summary="slept and passed")


def _sleep_plan(tmp_path, secs="0.6"):
    return Plan(kind="sim", adapter="slow", workdir=str(tmp_path),
                steps=[Step(argv=["sleep", secs], cwd=str(tmp_path))])


def test_submit_async_returns_before_the_job_finishes(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    t0 = time.time()
    rec = r.submit_async(_sleep_plan(tmp_path), _SlowAdapter(),
                         input_files=[])
    assert time.time() - t0 < 0.4      # did not block on the sleep
    assert rec.status == "running" and rec.detached is True
    done = r.wait(rec.id, timeout=30)
    assert done.status == "passed" and done.summary == "slept and passed"


def test_a_detached_job_is_on_disk_before_it_finishes(tmp_path):
    """A crash between launch and completion must leave a job that can be
    found, not one that silently never existed."""
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    rec = r.submit_async(_sleep_plan(tmp_path), _SlowAdapter(), input_files=[])
    on_disk = JobRunner(str(tmp_path / "runs"), registry=None).get(rec.id)
    assert on_disk is not None and on_disk.status == "running"
    assert r.pending() and r.pending()[0].id == rec.id
    r.wait(rec.id, timeout=30)
    assert r.pending() == []


def test_poll_does_not_block(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    rec = r.submit_async(_sleep_plan(tmp_path), _SlowAdapter(), input_files=[])
    t0 = time.time()
    assert r.poll(rec.id).status == "running"
    assert time.time() - t0 < 0.3
    r.wait(rec.id, timeout=30)


def test_a_wait_timeout_is_not_a_failure(tmp_path):
    """The job keeps running; reporting it as failed would be a lie that the
    gates would then happily act on."""
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    rec = r.submit_async(_sleep_plan(tmp_path, "3"), _SlowAdapter(),
                         input_files=[])
    assert r.wait(rec.id, timeout=0.2) is None
    assert r.poll(rec.id).status == "running"
    r.wait(rec.id, timeout=30)


def test_a_crashing_detached_job_records_the_crash(tmp_path):
    class _Boom(_SlowAdapter):
        def parse(self, plan, results):
            raise RuntimeError("adapter exploded")
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    rec = r.submit_async(_sleep_plan(tmp_path, "0.1"), _Boom(), input_files=[])
    done = r.wait(rec.id, timeout=30)
    assert done.status == "error" and "adapter exploded" in done.summary


def test_a_running_job_can_never_satisfy_a_gate():
    """The structural safety net: detaching must not become a way to report
    done on work that has not happened."""
    from chipchamp.jobs.cache import REUSABLE
    from chipchamp.policy.gates import Evidence, _passed_job
    from chipchamp.jobs.record import JobRecord
    running = JobRecord(id="J-1", kind="sim", adapter="a", status="running")
    assert _passed_job(Evidence(jobs=[running]), ["sim"]) is None
    assert "running" not in REUSABLE


def test_sim_run_detach_reports_that_nothing_is_known_yet():
    import inspect

    from chipchamp.tools import verif_tools
    src = inspect.getsource(verif_tools.sim_run)
    assert "detach=True" in src
    assert "do not report on it" in src


def test_the_loop_parks_the_session_on_pending_jobs():
    import inspect

    from chipchamp.agent import loop as loop_mod
    src = inspect.getsource(loop_mod.AgentLoop.run)
    assert 'self.session.task_frame["pending_jobs"]' in src
    assert '"pending_jobs": list(self.ctx.pending_jobs)' in src
