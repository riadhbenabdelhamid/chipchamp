"""Reasoning-effort lever (`/effort`): capability table, persistence, the
resolve_effort decision point, and the wire contract — an unset effort must be
byte-identical to the pre-feature payload, since a non-reasoning server may
reject an unknown field."""
from __future__ import annotations

import pytest

from chipchamp.agent.effort import (LEVELS, load_effort, resolve_effort,
                                   save_effort, supported_levels)


# ---- capability table ---------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("lmstudio:openai/gpt-oss-120b", ["low", "medium", "high"]),
    ("lmstudio:openai/gpt-oss-20b", ["low", "medium", "high"]),
    ("openai:o3-mini", ["low", "medium", "high"]),
    ("openai:prod-o3-mini", ["low", "medium", "high"]),   # dash-prefixed alias
    ("openai:gpt-5", ["minimal", "low", "medium", "high"]),
])
def test_reasoning_models_expose_their_ladder(ref, expected):
    assert supported_levels(ref) == expected


@pytest.mark.parametrize("ref", [
    "lmstudio:mistralai/devstral-small-2-2512",
    "lmstudio:qwen/qwen3.6-35b-a3b",
    "anthropic:claude-opus-4-8",
    "openai:gpt-4o",                      # '4o' must not read as o-series
    "lmstudio:x/somegpt-5.8b",            # merely CONTAINS 'gpt-5'
    "",
])
def test_non_reasoning_models_have_no_effort_control(ref):
    assert supported_levels(ref) is None


def test_config_can_declare_support_for_new_models():
    cfg = {"model": {"effort_support": {"deepseek-r1": ["low", "high"]}}}
    assert supported_levels("lmstudio:deepseek-r1-32b", cfg) == ["low", "high"]
    assert supported_levels("lmstudio:deepseek-r1-32b") is None  # built-ins alone
    # a broken user pattern is skipped, never fatal
    bad = {"model": {"effort_support": {"[": ["low"]}}}
    assert supported_levels("lmstudio:openai/gpt-oss-120b", bad) == \
        ["low", "medium", "high"]


def test_ladder_is_ordered_shallow_to_deep():
    assert LEVELS == ["low", "medium", "high"]


# ---- persistence --------------------------------------------------------------

def test_effort_round_trips_and_clears(tmp_path):
    assert load_effort(tmp_path) == ""          # unset by default
    save_effort(tmp_path, "high")
    assert load_effort(tmp_path) == "high"
    save_effort(tmp_path, "")                   # clear
    assert load_effort(tmp_path) == ""
    assert not (tmp_path / "effort.json").exists()


def test_corrupt_effort_json_degrades_to_unset(tmp_path):
    # this loads inside every gateway build (a startup path): a hand-edited or
    # truncated file must degrade, never crash the session
    (tmp_path / "effort.json").write_text("high")          # not JSON
    assert load_effort(tmp_path) == ""
    (tmp_path / "effort.json").write_text('["high"]')      # non-dict JSON
    assert load_effort(tmp_path) == ""
    (tmp_path / "effort.json").write_text("")              # truncated
    assert load_effort(tmp_path) == ""


# ---- resolve_effort: the single decision point ---------------------------------

GPTOSS = "lmstudio:openai/gpt-oss-120b"
DEVSTRAL = "lmstudio:mistralai/devstral-small-2-2512"


@pytest.fixture()
def no_env(monkeypatch):
    monkeypatch.delenv("CHIPCHAMP_MODEL_REASONING_EFFORT", raising=False)


def test_saved_effort_sent_only_to_supporting_models(tmp_path, no_env):
    save_effort(tmp_path, "high", ref=GPTOSS)
    assert resolve_effort(tmp_path, GPTOSS) == "high"
    assert resolve_effort(tmp_path, "lmstudio:openai/gpt-oss-20b") == "high"
    assert resolve_effort(tmp_path, DEVSTRAL) == ""   # gated, can't 400


def test_forced_effort_survives_only_on_the_exact_ref(tmp_path, no_env):
    unknown = "lmstudio:some/mystery-reasoner"
    save_effort(tmp_path, "high", ref=unknown)        # /effort forced it here
    assert resolve_effort(tmp_path, unknown) == "high"
    assert resolve_effort(tmp_path, DEVSTRAL) == ""   # not forced there


def test_off_pins_effort_off_masking_env_and_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPCHAMP_MODEL_REASONING_EFFORT", "high")
    cfg = {"model": {"reasoning_effort": "medium"}}
    save_effort(tmp_path, "off")
    assert resolve_effort(tmp_path, GPTOSS, cfg) == ""


def test_env_and_config_effort_normalized_and_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPCHAMP_MODEL_REASONING_EFFORT", "HIGH")
    assert resolve_effort(tmp_path, GPTOSS) == "high"     # normalized
    assert resolve_effort(tmp_path, DEVSTRAL) == ""       # gated
    monkeypatch.delenv("CHIPCHAMP_MODEL_REASONING_EFFORT")
    cfg = {"model": {"reasoning_effort": "Medium"}}
    assert resolve_effort(tmp_path, GPTOSS, cfg) == "medium"
    assert resolve_effort(tmp_path, GPTOSS, {"model": {"reasoning_effort": "bogus"}}) == ""


def test_saved_effort_for_another_model_falls_back_to_env(tmp_path, monkeypatch):
    save_effort(tmp_path, "high", ref=GPTOSS)
    monkeypatch.setenv("CHIPCHAMP_MODEL_REASONING_EFFORT", "low")
    # gpt-5 supports 'high' via its ladder, saved level wins
    assert resolve_effort(tmp_path, "openai:gpt-5") == "high"
    # devstral supports nothing: saved gated, env gated too
    assert resolve_effort(tmp_path, DEVSTRAL) == ""


# ---- wire contract ------------------------------------------------------------

class _Rec:
    """Captures the payload the provider would POST."""

    def __init__(self):
        self.payload = None

    def __call__(self, path, payload, timeout=None):
        self.payload = payload
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {}}


def _provider(rec):
    from chipchamp.agent.providers.base import ProviderConfig
    from chipchamp.agent.providers.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(ProviderConfig(
        name="lmstudio", kind="openai", base_url="http://x/v1", local=True))
    p._post = rec
    return p


def test_unset_effort_sends_no_field():
    rec = _Rec()
    _provider(rec).chat("m", "sys", [{"role": "user", "content": "hi"}], [])
    assert "reasoning_effort" not in rec.payload   # wire-identical to before


def test_set_effort_is_sent_on_the_wire():
    rec = _Rec()
    _provider(rec).chat("m", "sys", [{"role": "user", "content": "hi"}], [],
                        reasoning_effort="high")
    assert rec.payload["reasoning_effort"] == "high"


def test_gateway_threads_effort_to_the_provider():
    from chipchamp.agent.gateway import ModelGateway
    rec = _Rec()
    gw = ModelGateway(_provider(rec), "openai/gpt-oss-120b", reasoning_effort="low")
    gw.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert rec.payload["reasoning_effort"] == "low"
    # /effort retunes the live gateway: next call must use the new level
    gw.reasoning_effort = "high"
    gw.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert rec.payload["reasoning_effort"] == "high"


def test_openai_reasoning_models_get_max_completion_tokens():
    # OpenAI o-series/gpt-5 reject 'max_tokens'; everyone else requires it
    for model, key in (("o3-mini", "max_completion_tokens"),
                       ("gpt-5", "max_completion_tokens"),
                       ("openai/gpt-oss-120b", "max_tokens"),
                       ("mistralai/devstral-small-2-2512", "max_tokens")):
        rec = _Rec()
        _provider(rec).chat(model, "sys", [{"role": "user", "content": "hi"}], [],
                            max_tokens=1234)
        assert rec.payload.get(key) == 1234, model
        other = "max_tokens" if key != "max_tokens" else "max_completion_tokens"
        assert other not in rec.payload, model


# ---- router hand-off ----------------------------------------------------------

def test_router_carries_effort_only_to_a_model_that_supports_it(no_env):
    from chipchamp.agent.gateway import ModelGateway
    from chipchamp.agent.router import ModelRouter, RouterConfig
    from chipchamp.agent.providers.registry import ModelRegistry

    r = ModelRouter(ModelRegistry({}, "."), RouterConfig(), {})
    like = ModelGateway(None, "openai/gpt-oss-120b", reasoning_effort="high")

    # -> another reasoning model: effort carries over
    gw = r.build_gateway("lmstudio:openai/gpt-oss-20b", like=like)
    assert gw is not None and gw.reasoning_effort == "high"

    # -> a non-reasoning model: dropped, so the switch can't 400
    gw = r.build_gateway("lmstudio:mistralai/devstral-small-2-2512", like=like)
    assert gw is not None and gw.reasoning_effort == ""


def test_router_reresolves_persisted_effort_after_a_lossy_hop(tmp_path, no_env):
    # One hop through a non-supporting model must NOT erase the user's saved
    # effort for later supporting models (the like-chain amnesia bug).
    from chipchamp.agent.gateway import ModelGateway
    from chipchamp.agent.router import ModelRouter, RouterConfig
    from chipchamp.agent.providers.registry import ModelRegistry

    save_effort(tmp_path, "high", ref=GPTOSS)
    r = ModelRouter(ModelRegistry({}, str(tmp_path)), RouterConfig(), {})
    # `like` is the devstral gateway from the previous hop: effort already ''
    like = ModelGateway(None, "mistralai/devstral-small-2-2512",
                        reasoning_effort="")
    gw = r.build_gateway("lmstudio:openai/gpt-oss-20b", like=like)
    assert gw is not None and gw.reasoning_effort == "high"


# ---- per-call timeout budget (regression) -------------------------------------
# The gateway's wall-clock deadline and the provider's HTTP socket timeout must
# be ONE budget. They diverged twice: _post hardcoded 300s while the gateway
# honoured CHIPCHAMP_MODEL_TIMEOUT (env path, fixed first), and then the config
# [model] request_timeout raised only the deadline while the socket stayed at
# the env default. The gateway now threads its budget to the provider.

def test_post_defaults_to_the_shared_budget_not_a_hardcoded_ceiling():
    import inspect
    from chipchamp.agent.providers.openai_compat import OpenAICompatProvider
    sig = inspect.signature(OpenAICompatProvider._post)
    # must not re-introduce a hardcoded ceiling below the shared budget
    assert sig.parameters["timeout"].default is None


def test_post_uses_shared_budget_when_no_timeout_given(monkeypatch):
    from chipchamp.agent.providers import openai_compat as oc
    from chipchamp.agent.providers.base import ProviderConfig

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(oc, "DEFAULT_REQUEST_TIMEOUT", 1200.0)
    p = oc.OpenAICompatProvider(ProviderConfig(
        name="lmstudio", kind="openai", base_url="http://x/v1", local=True))
    with pytest.raises(RuntimeError):
        p._post("/chat/completions", {})
    assert seen["timeout"] == 1200.0   # not the old hardcoded 300


def test_gateway_budget_reaches_the_socket(monkeypatch):
    # config [model] request_timeout -> ModelGateway(timeout=...) -> socket
    from chipchamp.agent.gateway import ModelGateway
    from chipchamp.agent.providers import openai_compat as oc
    from chipchamp.agent.providers.base import ProviderConfig

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    p = oc.OpenAICompatProvider(ProviderConfig(
        name="lmstudio", kind="openai", base_url="http://x/v1", local=True))
    gw = ModelGateway(p, "m", timeout=1234)
    with pytest.raises(RuntimeError):
        gw.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert seen["timeout"] == 1234.0
