"""Live plan checklist (plan.update) + plan-mode approval gate (SPEC §8.2)."""
from __future__ import annotations

from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse
from chipchamp.tools import all_tools


# ---- plan tool ---------------------------------------------------------------


def test_plan_update_normalizes_and_stores(ctx):
    r = all_tools()["plan.update"].handler(ctx, steps=[
        {"step": "explore", "status": "done"},
        {"step": "edit", "status": "in_progress"},
        "verify",                                    # bare string -> pending
        {"step": "", "status": "pending"},           # empty dropped
    ])
    assert r["total"] == 3 and r["done"] == 1
    assert ctx.plan[2] == {"step": "verify", "status": "pending"}
    # bad status coerced to pending
    r2 = all_tools()["plan.update"].handler(ctx, steps=[{"step": "x", "status": "bogus"}])
    assert ctx.plan[0]["status"] == "pending"


def test_plan_persisted_to_task_frame(ctx, tmp_path):
    from chipchamp.agent.session import Session

    class G:
        available = True
        ref = "fake:x"

        def __init__(self):
            self.n = 0

        def complete(self, system, transcript, tools, audit_items=None):
            self.n += 1
            if self.n == 1:
                return ModelResponse(text="", tool_calls=[{
                    "id": "1", "name": "plan__update",
                    "input": {"steps": [{"step": "do it", "status": "in_progress"}]}}])
            return ModelResponse(text="done", tool_calls=[])

    sess = Session.new(str(tmp_path))
    loop = AgentLoop(ctx, G(), session=sess)
    loop.run("go")
    assert sess.task_frame.get("plan") == [{"step": "do it", "status": "in_progress"}]


# ---- plan-mode approval gate -------------------------------------------------


def _gated_loop(ctx, approver, script):
    class G:
        available = True
        ref = "fake:x"

        def __init__(self):
            self.script = list(script)

        def complete(self, *a, **k):
            return self.script.pop(0)

    return AgentLoop(ctx, G(), approver=approver,
                     gate_permissions={"write", "submit"})


def test_denied_write_does_not_touch_disk(ctx, workspace):
    """Plan mode: a declined fs.write must NOT create the file; the model gets a
    'denied' result to replan on."""
    calls = []
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "fs__write",
            "input": {"path": "rtl/new_mod.sv", "content": "module new_mod; endmodule"}}]),
        ModelResponse(text="ok, I won't write it", tool_calls=[]),
    ]
    loop = _gated_loop(ctx, lambda n, a, p: (calls.append(n), False)[1], script)
    results = []
    loop.on_event = lambda k, d: results.append((k, d)) if k == "tool_result" else None
    loop.run("add a module")
    assert calls == ["fs.write"]  # the approver WAS consulted
    import os
    assert not os.path.exists(os.path.join(workspace.root, "rtl", "new_mod.sv"))
    denied = [d for k, d in results if d.get("result", {}).get("denied")]
    assert denied and denied[0]["result"]["tool"] == "fs.write"


def test_approved_write_executes(ctx, workspace):
    import os
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "fs__write",
            "input": {"path": "rtl/ok_mod.sv", "content": "module ok_mod; endmodule\n"}}]),
        ModelResponse(text="written", tool_calls=[]),
    ]
    loop = _gated_loop(ctx, lambda n, a, p: True, script)
    loop.run("add a module")
    assert os.path.exists(os.path.join(workspace.root, "rtl", "ok_mod.sv"))
    os.remove(os.path.join(workspace.root, "rtl", "ok_mod.sv"))


def test_read_tools_not_gated(ctx):
    """A read-only tool must run without approval even in plan mode."""
    approvals = []
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "design__module",
            "input": {"module": "sync_fifo"}}]),
        ModelResponse(text="done", tool_calls=[]),
    ]
    loop = _gated_loop(ctx, lambda n, a, p: approvals.append(n) or True, script)
    loop.run("describe it")
    assert approvals == []  # design.module (read) never hit the gate


def test_report_done_excluded_from_gate(ctx):
    approvals = []
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "report__done",
            "input": {"summary": "x"}}]),
        ModelResponse(text="done", tool_calls=[]),
    ]
    loop = _gated_loop(ctx, lambda n, a, p: approvals.append(n) or False, script)
    loop.run("finish")
    assert "report.done" not in approvals  # finishing is never gated


def test_plan_update_tolerates_string_encoded_steps(ctx):
    """A real local-model failure: steps arrived JSON-encoded as a string and
    the plan exploded into per-character steps. Decode instead."""
    from chipchamp.tools import all_tools
    r = all_tools()["plan.update"].handler(
        ctx, steps='[{"step": "run deadwidth", "status": "in_progress"}, '
                   '{"step": "report"}]')
    assert r["total"] == 2
    assert r["plan"][0]["step"] == "run deadwidth"
    # non-JSON string becomes one step, not N characters
    r2 = all_tools()["plan.update"].handler(ctx, steps="just one step")
    assert r2["total"] == 1 and r2["plan"][0]["step"] == "just one step"
