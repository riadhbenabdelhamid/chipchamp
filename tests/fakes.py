"""Shared test doubles. Eight test files each carried their own copy of a
"pop the next scripted response" gateway (and two of a timeout gateway, and
several of a tmp-workspace builder); they had already drifted in what they
recorded. One definition, imported everywhere."""
from __future__ import annotations

from chipchamp.agent.gateway import ModelTimeout
from chipchamp.agent.providers.base import ModelResponse


class ScriptedGateway:
    """Returns scripted ModelResponses in order and records what it was asked:
    `audit_calls` (per call), `systems` (per call), `transcripts` (the LAST
    call's transcript). When the script runs out, `default` is returned if
    given; otherwise an IndexError marks a test that scripted too few turns."""
    available = True
    model = "fake"

    def __init__(self, script=(), ref: str = "fake:test", default: ModelResponse | None = None):
        self.script = list(script)
        self.ref = ref
        self.default = default
        self.audit_calls: list = []
        self.systems: list[str] = []
        self.transcripts: list[dict] = []
        # attributes the router / timeout plumbing reads on a real gateway
        self.max_tokens = 16384
        self.timeout = None
        self.audit = None

    def complete(self, system, transcript, tools, audit_items=None):
        self.audit_calls.append(audit_items or [])
        self.systems.append(system)
        self.transcripts = [dict(t) for t in transcript]
        if not self.script and self.default is not None:
            return self.default
        return self.script.pop(0)


FakeGateway = ScriptedGateway


class RefGateway(ScriptedGateway):
    """A ScriptedGateway with a distinct ref, so the router can tell models apart."""

    def __init__(self, script, ref):
        super().__init__(script, ref=ref)


class TimeoutGateway(RefGateway):
    def complete(self, *a, **k):
        raise ModelTimeout("model call exceeded 1s")


def tmp_ctx(tmp_path, files: dict | None = None, config: str = "[project]\nname='t'\n"):
    """A ToolContext over a throwaway workspace: .chipchamp/config.toml plus
    any `files` ({relative path: text})."""
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "config.toml").write_text(config)
    for rel, text in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return ToolContext(Workspace(str(tmp_path)))
