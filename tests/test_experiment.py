"""Wave 0 — the instrument: experiment recording and autopsy.

The recorder's job is to be *passive and honest*: it must not change the run it
measures, and `outcome` must never call a confident paragraph a completion.
"""
from __future__ import annotations

import json

from chipchamp.agent.loop import transcript_size
from chipchamp.bench.experiment import (ExperimentRecorder, autopsy,
                                        load_experiments)


class _Loop:
    """Minimal stand-in for AgentLoop: just the callback surface + gateway ref."""

    def __init__(self):
        self.seen = []
        self.on_event = lambda kind, data: self.seen.append(kind)
        self.gateway = type("G", (), {"ref": "ollama:qwen3-coder:30b"})()


def _drive(rec, *, done=True):
    rec.observe("thinking_start", {"model": "m", "chars": 1200, "messages": 3})
    rec.observe("thinking_done", {"input_tokens": 900, "output_tokens": 120})
    rec.observe("tool_call", {"name": "lint.run", "input": {}})
    rec.observe("job_done", {"name": "lint.run",
                             "result": {"job": "J-0001", "status": "passed"}})
    rec.observe("tool_result", {"name": "lint.run", "result": {"errors": 0}})
    rec.observe("thinking_start", {"model": "m", "chars": 4800, "messages": 5})
    rec.observe("thinking_done", {"input_tokens": 1500, "output_tokens": 80})
    rec.observe("tool_call", {"name": "fs.write", "input": {}})
    rec.observe("tool_result", {"name": "fs.write", "result": {"error": "denied"}})
    if done:
        rec.observe("tool_call", {"name": "report.done", "input": {}})
        rec.observe("tool_result", {"name": "report.done",
                                    "result": {"accepted": True}})


def test_recorder_chains_the_callback_instead_of_replacing_it(tmp_path):
    """A recorded run must render exactly like an unrecorded one — the REPL's
    own event handler has to keep firing."""
    loop = _Loop()
    rec = ExperimentRecorder(tmp_path, label="chain", task="t")
    rec.attach(loop)
    loop.on_event("thinking_start", {"model": "m", "chars": 10, "messages": 1})
    assert loop.seen == ["thinking_start"]      # original handler still ran
    # and the model was picked up off the gateway when not passed explicitly
    assert rec.exp.model == "ollama:qwen3-coder:30b"


def test_a_broken_observer_cannot_break_the_run(tmp_path):
    loop = _Loop()
    rec = ExperimentRecorder(tmp_path, label="safe", task="t")
    rec.attach(loop)
    rec.observe = lambda *a, **k: 1 / 0     # probe explodes
    loop.on_event("thinking_done", {})       # must not raise
    assert loop.seen == ["thinking_done"]


def test_metrics_and_outcome_are_recorded(tmp_path):
    rec = ExperimentRecorder(tmp_path, label="w0", task="lint the fifo",
                             model="ollama:devstral:24b")
    _drive(rec)
    exp = rec.finish({"steps": 3, "text": "done", "models_used": ["a"]},
                     max_steps=40)
    m = exp.metrics
    assert exp.outcome == "completed"          # report.done was accepted
    assert m["tokens_in"] == 2400 and m["tokens_out"] == 200
    assert m["context_chars_max"] == 4800      # the PEAK, not the last value
    assert m["model_calls"] == 2
    assert m["tools"]["fs.write"] == {"calls": 1, "failed": 1}
    assert m["jobs"] == 1 and m["jobs_passed"] == 1
    # written where the autopsy will look for it
    assert json.loads((tmp_path / "experiments" / f"{exp.id}.json").read_text())


def test_a_confident_paragraph_is_not_a_completion(tmp_path):
    """`completed` is reserved for report.done — the platform's own definition
    of done. Prose alone is `answered`, and an autopsy must be able to tell."""
    rec = ExperimentRecorder(tmp_path, label="w0", task="t")
    _drive(rec, done=False)
    exp = rec.finish({"steps": 4, "text": "I have fixed everything."})
    assert exp.outcome == "answered"
    assert exp.metrics["reported_done"] is False


def test_outcomes_distinguish_the_ways_a_run_dies(tmp_path):
    def outcome(result, **kw):
        r = ExperimentRecorder(tmp_path, label="x", task="t")
        return r.finish(result, save=False, **kw).outcome

    assert outcome({"error": True, "text": "boom"}) == "error"
    assert outcome({"timed_out": True, "text": ""}) == "timeout"
    assert outcome({"steps": 40, "text": "..."}, max_steps=40) == "capped"
    assert outcome({"steps": 2, "text": ""}) == "stalled"


def test_autopsy_ranks_models_and_keeps_n_visible(tmp_path):
    for i, (model, done) in enumerate([("fast:4b", False), ("fast:4b", False),
                                       ("good:30b", True)]):
        rec = ExperimentRecorder(tmp_path, label="cmp", task=f"t{i}", model=model)
        _drive(rec, done=done)
        rec.finish({"steps": 3, "text": "x"})
    rep = autopsy(load_experiments(tmp_path))
    assert rep["runs"] == 3
    assert rep["by_model"]["good:30b"]["completion_rate"] == 1.0
    assert rep["by_model"]["fast:4b"]["completion_rate"] == 0.0
    assert rep["by_model"]["fast:4b"]["runs"] == 2     # n travels with the rate
    assert rep["tool_failures"]["fs.write"] == 3       # every run failed it


def test_a_corrupt_record_does_not_blind_the_autopsy(tmp_path):
    rec = ExperimentRecorder(tmp_path, label="ok", task="t", model="m")
    _drive(rec)
    rec.finish({"steps": 1, "text": "x"})
    (tmp_path / "experiments" / "E-truncated.json").write_text("{oh no")
    assert len(load_experiments(tmp_path)) == 1


def test_transcript_size_counts_tool_results_and_calls():
    """Tool results dominate a hardware transcript — a size that ignored them
    would report a flat line while the context actually filled up."""
    t = [{"role": "user", "content": "hello"},
         {"role": "assistant", "content": "hi",
          "tool_calls": [{"input": {"test": "fifo_smoke"}}]},
         {"role": "tool", "content": [{"output": "x" * 500}]}]
    size = transcript_size(t)
    assert size["messages"] == 3
    assert size["chars"] > 500          # the 500-char tool result is counted
    assert transcript_size([])["chars"] == 0
