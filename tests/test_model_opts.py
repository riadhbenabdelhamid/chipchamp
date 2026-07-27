"""`/model tune` — per-model generation options (SPEC §14).

The store round-trips, layers config < global < per-ref, strips request-shaping
keys, and actually reaches the provider's wire payload."""
from __future__ import annotations

from chipchamp.agent import model_opts as mo


def test_parse_kv_coercion():
    setk, unset = mo.parse_kv(
        ["num_ctx=8192", "temperature=0.7", "verbose=true",
         'stop=["</s>"]', "name=foo", "gone=", "bare"])
    assert setk == {"num_ctx": 8192, "temperature": 0.7, "verbose": True,
                    "stop": ["</s>"], "name": "foo"}
    assert set(unset) == {"gone", "bare"}


def test_roundtrip_precedence_and_unset(tmp_path):
    d = str(tmp_path)
    mo.set_opts(d, "p:m", {"temperature": 0.2, "num_ctx": 4096}, [])
    mo.set_opts(d, mo.GLOBAL, {"seed": 1, "temperature": 0.9}, [], glob=True)
    eff = mo.resolve_opts(d, "p:m")
    assert eff["seed"] == 1 and eff["num_ctx"] == 4096
    assert eff["temperature"] == 0.2          # per-ref beats global
    mo.set_opts(d, "p:m", {}, ["num_ctx"])    # clear one per-ref key
    assert "num_ctx" not in mo.resolve_opts(d, "p:m")
    mo.reset(d, "p:m")                         # per-ref cleared, global remains
    assert mo.resolve_opts(d, "p:m") == {"seed": 1, "temperature": 0.9}


def test_config_options_are_lowest_priority(tmp_path):
    d = str(tmp_path)
    cfg = {"model": {"options": {"top_p": 0.5, "num_ctx": 1024}}}
    mo.set_opts(d, mo.GLOBAL, {"num_ctx": 2048}, [], glob=True)
    eff = mo.resolve_opts(d, "p:m", cfg)
    assert eff["top_p"] == 0.5        # from config
    assert eff["num_ctx"] == 2048     # global overrides config


def test_reserved_keys_are_stripped(tmp_path):
    d = str(tmp_path)
    mo.set_opts(d, mo.GLOBAL,
                {"model": "HACK", "messages": "x", "temperature": 0.1}, [],
                glob=True)
    eff = mo.resolve_opts(d, "p:m")
    assert "model" not in eff and "messages" not in eff
    assert eff["temperature"] == 0.1


def test_missing_file_degrades_to_empty(tmp_path):
    assert mo.load_state(str(tmp_path)) == {}
    assert mo.resolve_opts(str(tmp_path), "p:m") == {}


def test_gateway_threads_options_to_provider():
    from chipchamp.agent.gateway import ModelGateway
    from chipchamp.agent.providers.base import ModelResponse

    class CapProv:
        name = "lmstudio"
        request_timeout = None

        def __init__(self):
            self.kw = None

        def available(self):
            return True

        def chat(self, model, system, transcript, tools, **kw):
            self.kw = kw
            return ModelResponse(text="ok")

    p = CapProv()
    gw = ModelGateway(p, "m", options={"num_ctx": 8192})
    gw.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert p.kw["options"] == {"num_ctx": 8192}

    # unset options must stay wire-identical to before (no options kw)
    p2 = CapProv()
    ModelGateway(p2, "m").complete("s", [{"role": "user", "content": "x"}], [])
    assert "options" not in p2.kw


def test_model_tune_help_lists_ollama_and_lmstudio_params(capsys):
    from chipchamp.cli import _model_tune_help, _tune_epilog
    _model_tune_help()
    out = capsys.readouterr().out
    for key in ("num_ctx", "num_predict", "mirostat", "temperature", "top_k",
                "repeat_penalty", "response_format"):
        assert key in out, f"/model tune --help omits {key}"
    assert "Ollama" in out and "LM Studio" in out
    epi = _tune_epilog()                    # CLI `model --help` epilog
    assert "num_ctx" in epi and "--tune KEY=VALUE" in epi


def test_openai_payload_merges_options_but_protects_reserved():
    from chipchamp.agent.providers import openai_compat as oc
    from chipchamp.agent.providers.base import ProviderConfig

    p = oc.OpenAICompatProvider(ProviderConfig(
        name="lmstudio", kind="openai", base_url="http://x/v1", local=True))
    cap = {}

    def fake_post(path, payload, timeout=None):
        cap["payload"] = payload
        return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {}}

    p._post = fake_post
    p.chat("gpt-oss-20b", "s", [{"role": "user", "content": "q"}], [],
           max_tokens=50, options={"num_ctx": 8192, "temperature": 0.3,
                                   "model": "HACK", "messages": "HACK"})
    pl = cap["payload"]
    assert pl["num_ctx"] == 8192 and pl["temperature"] == 0.3
    assert pl["model"] == "gpt-oss-20b"          # reserved, not clobbered
    assert isinstance(pl["messages"], list)      # reserved, not clobbered
