"""Autonomy modes (item 3) + session resume (item 6)."""
from __future__ import annotations

import time
from pathlib import Path

from chipchamp.agent.session import Session


class _Loop:
    """Minimal stand-in exposing what _AutonomyMode mutates."""
    def __init__(self):
        self.gate_permissions = set()
        self.system_suffix = ""
        self.session = None


# ---- modes ---------------------------------------------------------------------


def test_mode_cycle_order_and_gating():
    from chipchamp.cli import _AutonomyMode, _MODE_ORDER
    loop = _Loop()
    m = _AutonomyMode(loop, "normal")
    assert m.name == "normal" and loop.gate_permissions == set()
    # normal -> auto -> plan -> normal
    assert m.cycle() == "auto" and loop.gate_permissions == set()
    assert m.cycle() == "plan"
    assert loop.gate_permissions == {"write", "submit"}  # plan gates writes/jobs
    assert "PLAN MODE" in loop.system_suffix
    assert m.cycle() == "normal" and loop.gate_permissions == set()
    assert _MODE_ORDER == ["normal", "auto", "plan"]


def test_mode_set_explicit():
    from chipchamp.cli import _AutonomyMode
    loop = _Loop()
    m = _AutonomyMode(loop, "normal")
    assert m.set("plan") and loop.gate_permissions == {"write", "submit"}
    assert m.set("auto") and loop.gate_permissions == set()
    assert not m.set("bogus") and m.name == "auto"  # unknown ignored


def test_plan_flag_maps_to_plan_mode():
    from chipchamp.cli import _AutonomyMode
    loop = _Loop()
    m = _AutonomyMode(loop, "plan")
    assert m.name == "plan" and loop.gate_permissions == {"write", "submit"}


# ---- sessions ------------------------------------------------------------------


def _write_session(store: Path, sid: str, msgs, when: float, target="soc"):
    store.mkdir(parents=True, exist_ok=True)
    s = Session(sid, str(store), target=target)
    s.messages = msgs
    s.save()
    import os
    os.utime(store / f"{sid}.json", (when, when))


def test_latest_and_summaries(tmp_path):
    store = tmp_path / "sessions"
    _write_session(store, "S-1000-aaaa",
                   [{"role": "user", "content": "first task about a fifo"}], 1000)
    _write_session(store, "S-2000-bbbb",
                   [{"role": "user", "content": "second task about an arbiter"},
                    {"role": "assistant", "content": "ok"}], 2000)
    latest = Session.latest(str(store))
    assert latest.id == "S-2000-bbbb"
    sums = Session.summaries(str(store))
    assert [r["id"] for r in sums] == ["S-2000-bbbb", "S-1000-aaaa"]  # newest first
    assert sums[0]["messages"] == 2
    assert "arbiter" in sums[0]["preview"]
    assert Session.latest(str(tmp_path / "empty")) is None


def test_resume_loads_transcript(tmp_path):
    store = tmp_path / "sessions"
    _write_session(store, "S-1234-cccc",
                   [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"}], 1234)
    s = Session.load("S-1234-cccc", str(store))
    assert s is not None and len(s.messages) == 2
    assert s.messages[0]["content"] == "hi"


def test_sessions_cmd_resume_swaps_loop_session(tmp_path, capsys):
    from chipchamp.cli import _sessions_cmd

    class C:
        class ws:
            dot = tmp_path
    store = tmp_path / "sessions"
    _write_session(store, "S-5-dddd", [{"role": "user", "content": "x"}], 5)

    loop = _Loop()
    loop.session = Session.new(str(store))
    _sessions_cmd(C, loop, ["resume", "S-5-dddd"])
    assert loop.session.id == "S-5-dddd"


# ---- resume: replay + up/down history (this session's asks) ---------------------

# a session mixing genuine user turns (input: True) with the loop's synthetic
# "user" nudge and a tool turn — resume must keep the two apart.
_MIXED = [
    {"role": "user", "content": "why does fifo_smoke fail?", "input": True},
    {"role": "assistant", "content": "Let me check the waveform."},
    {"role": "tool", "content": [{"id": "j1", "name": "sim__run", "output": "FAIL"}]},
    {"role": "user", "content": "You ended with a message but no tool call — act."},
    {"role": "assistant", "content": "Root cause: a CDC on seed 3."},
    {"role": "user", "content": "fix it please", "input": True},
]


def test_session_user_inputs_excludes_synthetic_nudges(tmp_path):
    from chipchamp.cli import _session_user_inputs
    store = tmp_path / "sessions"
    _write_session(store, "S-7-eeee", _MIXED, 7)
    s = Session.load("S-7-eeee", str(store))
    # only the two genuine requests, oldest→newest — never the nudge
    assert _session_user_inputs(s) == ["why does fifo_smoke fail?", "fix it please"]


def test_session_user_inputs_falls_back_for_unmarked_sessions(tmp_path):
    from chipchamp.cli import _session_user_inputs
    store = tmp_path / "sessions"
    _write_session(store, "S-8-ffff",
                   [{"role": "user", "content": "old task"},
                    {"role": "assistant", "content": "done"}], 8)
    s = Session.load("S-8-ffff", str(store))
    assert _session_user_inputs(s) == ["old task"]  # no flag anywhere → show it


def test_replay_session_shows_conversation_not_nudges(tmp_path, capsys):
    from chipchamp.cli import _replay_session
    store = tmp_path / "sessions"
    _write_session(store, "S-9-gggg", _MIXED, 9)
    s = Session.load("S-9-gggg", str(store))
    _replay_session(s)
    out = capsys.readouterr().out
    assert "previous conversation" in out
    assert "why does fifo_smoke fail?" in out and "fix it please" in out
    assert "CDC on seed 3" in out            # assistant replies replayed
    assert "no tool call" not in out         # synthetic nudge hidden


def test_history_is_per_session_seeded_and_starts_clean():
    import pytest

    from chipchamp import ui
    if not ui._HAS_PTK:
        pytest.skip("prompt_toolkit not installed")
    from prompt_toolkit.history import InMemoryHistory
    ic = ui.InputController({})
    ic.history = InMemoryHistory()
    # a brand-new session starts EMPTY — no other sessions' commands bleed in
    assert list(ic.history.get_strings()) == []
    ic.remember(["why does fifo_smoke fail?", "fix it please"])  # seed a resume
    ic.remember(["fix it please"])                               # dedup: no-op
    got = ic.history.get_strings()
    assert sorted(got) == ["fix it please", "why does fifo_smoke fail?"]
    assert len(got) == 2                                         # no duplicate
