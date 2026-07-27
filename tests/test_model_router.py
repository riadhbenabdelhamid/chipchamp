"""Model router: capability ranking, fallback chains, config persistence,
refusal detection, and telemetry aggregation."""
from __future__ import annotations

import json

from chipchamp.agent.model_profiles import load_profiles, default_profile
from chipchamp.agent.providers.registry import ModelRegistry
from chipchamp.agent.router import (ModelRouter, RouterConfig, need_for_task,
                                   telemetry_stats)

QWEN = "lmstudio:qwen/qwen3.6-35b-a3b"
OSS120 = "lmstudio:openai/gpt-oss-120b"
OSS20 = "lmstudio:openai/gpt-oss-20b"      # failure_mode: refuses


def _router(pool, chains=None, overrides=None):
    cfg = RouterConfig(enabled=True, pool=list(pool), chains=chains or {},
                       profile_overrides=overrides or {})
    return ModelRouter(ModelRegistry({}, "."), cfg, {})


def test_ranking_reflects_campaign_strengths():
    r = _router([QWEN, OSS120])
    # gpt-oss-120b authors better; qwen debugs better
    assert r.pick("rtl_author") == OSS120
    assert r.pick("debugger") == QWEN
    assert r.chain("rtl_author") == [OSS120, QWEN]
    assert r.chain("debugger") == [QWEN, OSS120]


def test_refusing_model_is_demoted_below_capable_ones():
    r = _router([OSS20, QWEN])          # oss20 has failure_mode "refuses"
    # despite oss20 being "fast", the refusal demerit sinks it to last
    assert r.rank("rtl_author")[-1] == OSS20
    assert r.pick("rtl_author") == QWEN


def test_explicit_chain_overrides_scores():
    r = _router([QWEN, OSS120], chains={"rtl_author": [QWEN, OSS120]})
    assert r.chain("rtl_author") == [QWEN, OSS120]   # chain wins over profile


def test_next_fallback_order_and_exhaustion():
    r = _router([OSS120, QWEN])
    assert r.next_fallback(OSS120, "rtl_author", {OSS120}) == QWEN
    assert r.next_fallback(OSS120, "rtl_author", {OSS120, QWEN}) is None


def test_profile_override_changes_ranking():
    r = _router([QWEN, OSS120],
                overrides={OSS120: {"scores": {"debugger": 0.99},
                                    "failure_modes": []}})
    assert r.pick("debugger") == OSS120     # override lifts it above qwen


def test_is_refusal():
    r = _router([QWEN])
    assert r.is_refusal("I'm sorry, but I can't continue with this task.")
    assert r.is_refusal("I cannot continue.")
    assert not r.is_refusal("I can't find the FIFO depth, let me check lib.info "
                            "and then instantiate it with the right parameters "
                            "and continue building the datapath as planned here.")
    assert not r.is_refusal("")


def test_config_round_trip(tmp_path):
    cfg = RouterConfig(enabled=True, pool=[QWEN, OSS120])
    cfg.layers["telemetry"] = True
    cfg.triggers["stall"] = False
    cfg.chains["debugger"] = [QWEN]
    cfg.save(tmp_path)
    back = RouterConfig.load(tmp_path)
    assert back.enabled and back.pool == [QWEN, OSS120]
    assert back.layers["telemetry"] and back.triggers["stall"] is False
    assert back.chains["debugger"] == [QWEN]


def test_need_for_task_maps_classes():
    assert need_for_task("rtl_functional") == "rtl_author"
    assert need_for_task("cdc") == "debugger"
    assert need_for_task("unknown") == "rtl_author"


def test_profiles_merge_precedence():
    profs = load_profiles({"model": {"profiles": {QWEN: {"scores": {"speed": 0.9}}}}},
                          {QWEN: {"scores": {"speed": 0.1}}})
    assert profs[QWEN].score("speed") == 0.1        # router override wins
    assert profs[QWEN].score("debugger") == 0.9     # built-in preserved
    assert default_profile("anthropic:claude-sonnet-5").score("rtl_author") >= 0.85


def test_telemetry_stats_aggregates(tmp_path):
    cfg = RouterConfig(enabled=True)
    cfg.layers["telemetry"] = True
    r = ModelRouter(ModelRegistry({}, "."), cfg, {})
    r.record_outcome(tmp_path, {"task_class": "rtl_functional",
                                "models_used": [OSS120, QWEN], "completed": True})
    r.record_outcome(tmp_path, {"task_class": "rtl_functional",
                                "models_used": [OSS20], "refused": True})
    agg = telemetry_stats(tmp_path)
    assert agg[f"{QWEN}|rtl_functional"]["completed"] == 1
    assert agg[f"{OSS20}|rtl_functional"]["refused"] == 1


def test_telemetry_off_writes_nothing(tmp_path):
    r = ModelRouter(ModelRegistry({}, "."), RouterConfig(enabled=True), {})
    r.record_outcome(tmp_path, {"task_class": "x", "model": "m"})
    assert telemetry_stats(tmp_path) == {}
