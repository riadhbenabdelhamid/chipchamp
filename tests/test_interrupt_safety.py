"""Ctrl-C during a tool call must not leave the session unusable. The saved
transcript ended on an assistant turn with tool_calls and no results — both
providers reject that on every later call, and resuming the session failed
the same way."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from chipchamp.agent import Session
from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse
from chipchamp.agent.providers.openai_compat import to_openai_messages
from chipchamp.agent.session import close_dangling_tool_calls


class Gateway:
    available = True
    model = "fake"
    ref = "fake:test"

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def complete(self, system, transcript, tools, audit_items=None):
        self.seen.append([dict(m) for m in transcript])
        return self.script.pop(0)


def _slow_tool(raises):
    def handler(ctx, **kw):
        raise raises
    return NS(handler=handler, permission="read", description="slow",
              group="verify", cost="free",
              schema={"type": "object", "properties": {}})


def _legal(transcript):
    for a, b in zip(transcript, transcript[1:]):
        if a.get("role") == "assistant" and a.get("tool_calls"):
            assert b.get("role") == "tool", "tool_calls without results"


# ---- the pure repair ---------------------------------------------------------

def test_repair_answers_every_unanswered_call():
    t = [{"role": "user", "content": "go"},
         {"role": "assistant", "content": "", "tool_calls": [
             {"id": "a", "name": "sim__run", "input": {}},
             {"id": "b", "name": "lint__run", "input": {}}]}]
    assert close_dangling_tool_calls(t) == 1
    assert t[-1]["role"] == "tool" and [r["id"] for r in t[-1]["content"]] == ["a", "b"]
    assert "interrupted" in t[-1]["content"][0]["output"]
    assert close_dangling_tool_calls(t) == 0          # idempotent


def test_repair_handles_a_dangling_turn_in_the_middle():
    t = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "input": {}}]},
         {"role": "user", "content": "nudge appended after the interrupt"}]
    assert close_dangling_tool_calls(t) == 1
    assert [m["role"] for m in t] == ["assistant", "tool", "user"]


# ---- through the loop --------------------------------------------------------

def test_interrupt_mid_tool_leaves_a_legal_saved_session(ctx, tmp_path):
    sess = Session.new(str(tmp_path))
    gw = Gateway([ModelResponse(text="", tool_calls=[
        {"id": "c1", "name": "slow__run", "input": {}},
        {"id": "c2", "name": "slow__run", "input": {}}])])
    loop = AgentLoop(ctx, gw, session=sess, tools={"slow.run": _slow_tool(KeyboardInterrupt())})
    with pytest.raises(KeyboardInterrupt):
        loop.run("run the slow thing")
    saved = Session.load(sess.id, str(tmp_path))
    assert saved is not None
    _legal(saved.messages)
    last = saved.messages[-1]
    assert last["role"] == "tool" and [r["id"] for r in last["content"]] == ["c1", "c2"]
    assert all("interrupted" in r["output"] for r in last["content"])
    # the wire form is what the provider sees: every tool_call has its result
    wire = to_openai_messages("SYS", saved.messages)
    assert sum(1 for m in wire if m["role"] == "tool") == 2


def test_the_next_task_in_that_session_works(ctx, tmp_path):
    sess = Session.new(str(tmp_path))
    gw = Gateway([ModelResponse(text="", tool_calls=[{"id": "c1", "name": "slow__run", "input": {}}]),
                  ModelResponse(text="done", tool_calls=[])])
    loop = AgentLoop(ctx, gw, session=sess, tools={"slow.run": _slow_tool(KeyboardInterrupt())})
    with pytest.raises(KeyboardInterrupt):
        loop.run("first")
    out = loop.run("second")
    assert out["text"] == "done"
    _legal(gw.seen[-1])


def test_a_session_poisoned_by_an_older_build_is_repaired_on_resume(ctx, tmp_path):
    sess = Session.new(str(tmp_path))
    sess.messages = [{"role": "user", "content": "first", "input": True},
                     {"role": "assistant", "content": "", "tool_calls": [
                         {"id": "z", "name": "sim__run", "input": {}}]}]
    sess.save()
    gw = Gateway([ModelResponse(text="ok", tool_calls=[])])
    loop = AgentLoop(ctx, gw, session=Session.load(sess.id, str(tmp_path)))
    loop.run("second")
    _legal(gw.seen[0])
    assert gw.seen[0][2]["role"] == "tool"
