"""Total model-call timeout (gateway) + thinking spinner (ui) + loop wiring."""
from __future__ import annotations

import time

import pytest

from chipchamp import ui
from chipchamp.agent.gateway import ModelGateway, ModelTimeout, _call_with_deadline
from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse, Provider, ProviderConfig


class _SlowProvider(Provider):
    def __init__(self, delay):
        super().__init__(ProviderConfig(name="slow", kind="openai"))
        self.delay = delay

    def chat(self, model, system, transcript, tools, max_tokens=4096):
        time.sleep(self.delay)
        return ModelResponse(text="done", tool_calls=[])


# ---- deadline primitive ------------------------------------------------------


def test_call_with_deadline_times_out_fast():
    t0 = time.time()
    with pytest.raises(ModelTimeout):
        _call_with_deadline(lambda: time.sleep(5), timeout=0.3)
    assert time.time() - t0 < 2.0  # returned promptly, didn't wait the full 5s


def test_call_with_deadline_passes_result_and_errors():
    assert _call_with_deadline(lambda: 42, timeout=1) == 42

    def boom():
        raise ValueError("kaboom")
    with pytest.raises(ValueError, match="kaboom"):
        _call_with_deadline(boom, timeout=1)


# ---- gateway enforces the budget --------------------------------------------


def test_gateway_times_out_a_slow_provider():
    gw = ModelGateway(_SlowProvider(delay=5), "m", timeout=0.3)
    with pytest.raises(ModelTimeout):
        gw.complete("sys", [{"role": "user", "content": "hi"}], [])


def test_gateway_returns_when_fast():
    gw = ModelGateway(_SlowProvider(delay=0.0), "m", timeout=5)
    r = gw.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert r.text == "done"


# ---- loop surfaces timeout cleanly + emits thinking events -------------------


class _TimingOutGateway:
    available = True
    ref = "slow:model"

    def complete(self, system, transcript, tools, audit_items=None):
        raise ModelTimeout("model call exceeded 0s")


class _OkGateway:
    available = True
    ref = "fast:model"

    def complete(self, system, transcript, tools, audit_items=None):
        return ModelResponse(text="hello", tool_calls=[])


def test_loop_reports_timeout_with_switch_hint(ctx):
    events = []
    loop = AgentLoop(ctx, _TimingOutGateway(), on_event=lambda k, d: events.append((k, d)))
    out = loop.run("do it")
    assert out.get("error")
    assert "timed out" in out["text"] and "/model" in out["text"]
    kinds = [k for k, _ in events]
    # spinner is started and always stopped, even on the timeout path
    assert kinds.count("thinking_start") == kinds.count("thinking_done") == 1


def test_loop_emits_matched_thinking_events_on_success(ctx):
    events = []
    loop = AgentLoop(ctx, _OkGateway(), on_event=lambda k, d: events.append((k, d)))
    loop.run("hi")
    kinds = [k for k, _ in events]
    assert kinds.count("thinking_start") == kinds.count("thinking_done") == 1
    assert kinds.index("thinking_start") < kinds.index("thinking_done")


# ---- spinner -----------------------------------------------------------------


def test_thinking_spinner_noop_off_tty_and_safe():
    # in the test env stdin/stdout are not a TTY -> start() is a no-op, no thread
    t = ui.Thinking("thinking · x")
    t.start()
    assert t._thread is None
    t.stop()  # must be safe even when never started


# ---- sign of life: the task-scoped status line --------------------------------


def test_task_pulse_formats_the_status_line():
    """`✽ <task>… (4m 40s · ↓ 9.7k tokens · thinking with xhigh effort)` — the
    task in the user's own words, on a clock that spans the whole task."""
    import time
    p = ui.TaskPulse("why does fifo_smoke fail on seed 3?", effort="xhigh")
    p.add_tokens(9700)
    p.started = time.time() - 280
    assert p.status() == "4m 40s · ↓ 9.7k tokens · thinking with xhigh effort"
    # an in-flight call is estimated from characters, so the count says so
    assert p.status(live=412).startswith("4m 40s · ↓ ~10.1k tokens")
    # no effort configured -> no effort clause
    assert "effort" not in ui.TaskPulse("t").status()
    # a provider that reports no usage must not break the count
    p.add_tokens(None), p.add_tokens("x"), p.add_tokens(-5)
    assert p.tokens == 9700


def test_task_pulse_line_degrades_by_importance(monkeypatch):
    """Narrow terminals drop the effort clause before shortening the task name,
    and never overflow — the ticker repaints one row, so a wrapped line would
    stack spinners."""
    import time
    p = ui.TaskPulse("why does fifo_smoke fail on seed 3?", effort="xhigh")
    p.add_tokens(9700)
    p.started = time.time() - 280
    vis = lambda w: ui._ANSI.sub("", p.line(5, width=w))
    assert all(len(vis(w)) <= w for w in range(24, 140))     # never overflows
    assert "thinking with xhigh effort" in vis(110)
    assert "thinking with" not in vis(72) and "9.7k tokens" in vis(72)
    # the name never gets shorter as the terminal gets wider
    names = [vis(w).split("… (")[0] for w in (44, 56, 72, 110)]
    assert names == sorted(names, key=len)
    # too narrow for any of it: the clock alone still proves it is alive
    assert vis(28).strip().endswith("4m 40s")


def test_task_label_is_the_users_own_words():
    assert ui.task_label("add a skid buffer\nand test it") == "add a skid buffer"
    assert ui.task_label("  ") == "working"
    long = ui.task_label("why does the fifo_smoke testbench fail on seed 3?")
    assert long.startswith("why does the fifo") and long.endswith("…")
    assert len(long) <= 34


def test_stream_view_ticker_uses_the_task_pulse(monkeypatch):
    """The per-call spinner reports the TASK's clock, not its own: without it
    the timer restarts at every model call and a long task looks idle."""
    import time
    p = ui.TaskPulse("bring up the APB peripheral", effort="high")
    p.started = time.time() - 400
    sv = ui.StreamView("lmstudio:qwen3", pulse=p)
    sv._start = time.time() - 3          # this call is 3s old, the task is 400
    out = ui._ANSI.sub("", sv._ticker(3, 5))
    assert "6m 40s" in out and "bring up the APB peripheral" in out
    # with no task context (headless, sub-agents) it falls back to the model
    solo = ui._ANSI.sub("", ui.StreamView("lmstudio:qwen3")._ticker(3, 5))
    assert "lmstudio:qwen3" in solo and "3s" in solo


def test_agent_events_run_one_pulse_for_the_whole_task(ctx):
    """The REPL opens a pulse per task and the events feed it: each finished
    model call credits its EXACT output tokens, so only the in-flight call is
    ever estimated. Without the wiring the spinner silently shows no task."""
    from chipchamp.cli import _agent_events
    ev = _agent_events(ctx)
    p = ev.begin_task("why does fifo_smoke fail on seed 3?", "xhigh")
    assert p.task == "why does fifo_smoke fail on seed…"
    ev("thinking_done", {"output_tokens": 9700, "input_tokens": 1200})
    ev("thinking_done", {"output_tokens": 300})
    assert p.tokens == 10_000                      # exact, output only
    ev("thinking_done", {})                        # a call that reported none
    assert p.tokens == 10_000
    ev.end_task()
    # after the task the spinners go back to their own clocks
    ev("thinking_done", {"output_tokens": 5})      # must not raise
