"""Destructive actions (fs.delete / marked tools) always require explicit
human confirmation, independent of autonomy mode — item 8.

Uses a throwaway tmp workspace so a confirmed delete never touches the repo."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipchamp.agent.providers.base import ModelResponse
from chipchamp.agent.loop import AgentLoop
from chipchamp.tools import all_tools


class FakeGateway:
    available = True
    ref = "fake:test"
    model = "fake"

    def __init__(self, script):
        self.script = list(script)

    def complete(self, system, transcript, tools, audit_items=None):
        return self.script.pop(0)


@pytest.fixture()
def tmp_ctx(tmp_path):
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    dot = tmp_path / ".chipchamp"
    dot.mkdir()
    (dot / "config.toml").write_text("[project]\nname='t'\n")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "victim.sv").write_text("module victim; endmodule\n")
    return ToolContext(Workspace(str(tmp_path)))


def _delete_script(path):
    return [
        ModelResponse(text="", tool_calls=[
            {"id": "1", "name": "fs__delete", "input": {"path": path}}]),
        ModelResponse(text="done", tool_calls=[]),
    ]


def test_fs_delete_is_marked_destructive():
    assert all_tools()["fs.delete"].destructive is True
    assert all_tools()["fs.write"].destructive is False
    assert all_tools()["fs.edit"].destructive is False


def test_destructive_declined_leaves_file(tmp_ctx):
    victim = Path(tmp_ctx.ws.root) / "rtl" / "victim.sv"
    calls = []

    def approver(name, args, perm):
        calls.append((name, perm))
        return False  # human says no

    loop = AgentLoop(tmp_ctx, FakeGateway(_delete_script("rtl/victim.sv")),
                     approver=approver)
    out = loop.run("delete the victim")
    assert ("fs.delete", "destructive") in calls
    assert victim.exists(), "declined delete must not remove the file"
    assert out["text"] == "done"


def test_destructive_confirmed_deletes(tmp_ctx):
    victim = Path(tmp_ctx.ws.root) / "rtl" / "victim.sv"
    approvals = []

    def approver(name, args, perm):
        approvals.append(perm)
        return True  # human confirms

    loop = AgentLoop(tmp_ctx, FakeGateway(_delete_script("rtl/victim.sv")),
                     approver=approver)
    loop.run("delete the victim")
    assert "destructive" in approvals
    assert not victim.exists()


def test_destructive_gated_even_in_auto_mode(tmp_ctx):
    """No gate_permissions (auto/normal mode) — destructive still confirms."""
    victim = Path(tmp_ctx.ws.root) / "rtl" / "victim.sv"
    seen = {"asked": False}

    def approver(name, args, perm):
        seen["asked"] = perm == "destructive"
        return False

    loop = AgentLoop(tmp_ctx, FakeGateway(_delete_script("rtl/victim.sv")),
                     approver=approver, gate_permissions=set())  # auto mode
    loop.run("delete it")
    assert seen["asked"], "destructive op must confirm even with no plan gating"
    assert victim.exists()


def test_destructive_event_emitted(tmp_ctx):
    events = []
    loop = AgentLoop(tmp_ctx, FakeGateway(_delete_script("rtl/victim.sv")),
                     approver=lambda *a: False,
                     on_event=lambda k, d: events.append(k))
    loop.run("delete it")
    assert "destructive" in events


def test_default_approver_denies_destructive_offtty(monkeypatch):
    import chipchamp.cli as cli
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    approver = cli._make_approver()
    assert approver("fs.delete", {"path": "x"}, "destructive") is False


def test_destructive_not_double_gated_in_plan_mode(tmp_ctx):
    """A destructive tool that clears its confirm must NOT also be gated by
    plan mode — one prompt, not two (finding #7)."""
    calls = []

    def approver(name, args, perm):
        calls.append(perm)
        return True

    loop = AgentLoop(tmp_ctx, FakeGateway(_delete_script("rtl/victim.sv")),
                     approver=approver,
                     gate_permissions={"write", "submit"})  # plan mode
    loop.run("delete it")
    # exactly one approval, and it's the destructive one (not a second 'write')
    assert calls == ["destructive"]
