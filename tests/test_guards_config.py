"""`[agent.guards]` — the loop guards are reachable from config, not only from
a test harness. Headless runs demand report.done; interactive turns do not,
unless the workspace says so. Knob names must track AgentLoop's signature."""
from __future__ import annotations

import inspect

from chipchamp.agent.loop import AgentLoop
from chipchamp.policy import GuardConfig


def test_defaults_match_the_campaign_values():
    g = GuardConfig.from_config({})
    assert (g.max_narration_nudges, g.verify_every_n_writes,
            g.progress_guard_after) == (3, 3, 2)


def test_report_done_follows_the_mode_when_unset():
    assert GuardConfig.from_config({}, headless=False).require_report_done is False
    assert GuardConfig.from_config({}, headless=True).require_report_done is True


def test_explicit_report_done_wins_over_the_mode():
    cfg = {"agent": {"guards": {"require_report_done": False}}}
    assert GuardConfig.from_config(cfg, headless=True).require_report_done is False
    cfg = {"agent": {"guards": {"require_report_done": True}}}
    assert GuardConfig.from_config(cfg, headless=False).require_report_done is True


def test_values_are_read_and_clamped():
    cfg = {"agent": {"guards": {"max_narration_nudges": 0,
                                "verify_every_n_writes": -4,
                                "progress_guard_after": 5}}}
    g = GuardConfig.from_config(cfg)
    assert g.max_narration_nudges == 1          # the loop needs at least one
    assert g.verify_every_n_writes == 0         # 0 = guard off, never negative
    assert g.progress_guard_after == 5


def test_kwargs_name_real_loop_parameters():
    params = inspect.signature(AgentLoop.__init__).parameters
    for k in GuardConfig().as_kwargs():
        assert k in params, f"GuardConfig.{k} is not an AgentLoop parameter"


def test_workspace_exposes_guards(ctx):
    ctx.ws.config.setdefault("agent", {})["guards"] = {"verify_every_n_writes": 7}
    try:
        g = ctx.ws.guards(headless=True)
        assert g.verify_every_n_writes == 7
        assert g.require_report_done is True
        loop = AgentLoop(ctx, None, **g.as_kwargs())
        assert loop.verify_every_n_writes == 7 and loop.require_report_done
    finally:
        ctx.ws.config.pop("agent", None)
