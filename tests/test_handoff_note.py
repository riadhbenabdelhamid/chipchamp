"""A router handoff that fires right after tool results rides in the system
prompt — for exactly one model call. It was never cleared: every later call
of a long REPL session (one AgentLoop per session) said "[router] You (X)
have taken over…", naming the wrong model after a second switch."""
from __future__ import annotations

from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse


class SysCapture:
    available = True
    model = "fake"
    ref = "fake:test"

    def __init__(self, script):
        self.script = list(script)
        self.systems: list[str] = []

    def complete(self, system, transcript, tools, audit_items=None):
        self.systems.append(system)
        return self.script.pop(0)


def _text(t="ok"):
    return ModelResponse(text=t, tool_calls=[])


def test_note_rides_exactly_one_successful_call(ctx):
    gw = SysCapture([_text(), _text()])
    loop = AgentLoop(ctx, gw)
    tr = [{"role": "user", "content": "go"}]
    loop._handoff_note = "[router] You (m2) have taken over."
    loop._call_with_retry("SYS", tr)
    loop._call_with_retry("SYS", tr)
    assert "[router]" in gw.systems[0]
    assert "[router]" not in gw.systems[1]
    assert loop._handoff_note == ""


def test_note_survives_a_retry_of_the_same_call(ctx):
    class Flaky(SysCapture):
        def complete(self, system, transcript, tools, audit_items=None):
            self.systems.append(system)
            if len(self.systems) == 1:
                raise RuntimeError("HTTP 500 mid-stream")   # retryable
            return self.script.pop(0)
    gw = Flaky([_text()])
    loop = AgentLoop(ctx, gw)
    loop._handoff_note = "[router] handoff"
    import time
    orig = time.sleep
    time.sleep = lambda *_: None            # the retry back-off really sleeps
    try:
        loop._call_with_retry("SYS", [{"role": "user", "content": "go"}])
    finally:
        time.sleep = orig
    assert all("[router]" in s for s in gw.systems)   # both attempts saw it
    assert loop._handoff_note == ""


def test_a_new_run_never_inherits_a_stale_note(ctx):
    gw = SysCapture([_text("done")])
    loop = AgentLoop(ctx, gw)
    loop._handoff_note = "[router] stale from the previous task"
    loop.run("second task in the same REPL session")
    assert gw.systems and "[router]" not in gw.systems[0]


def test_fetched_set_is_per_loop_not_global(ctx):
    a, b = AgentLoop(ctx, SysCapture([])), AgentLoop(ctx, SysCapture([]))
    a.fetched_paths.add("work/rtl/lib/x.sv")
    assert "work/rtl/lib/x.sv" not in b.fetched_paths
