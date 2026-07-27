"""Per-model call budget (`/timeout`) + streaming keep-alive (SPEC §14).

Slow models: a per-model wall-clock budget, a content-liveness timeout that
doesn't kill a still-generating stream, and a `timed_out` flag the REPL uses to
offer a bigger-budget retry."""
from __future__ import annotations

import time

import pytest

from chipchamp.agent import model_timeout as mt


# ---- store + resolution --------------------------------------------------------


def test_resolution_precedence_and_off(tmp_path):
    d = str(tmp_path)
    assert mt.resolve_timeout(d, "p:m") is None            # nothing set → default
    mt.set_timeout(d, mt.GLOBAL, 120, glob=True)
    assert mt.resolve_timeout(d, "p:m") == 120.0           # global
    mt.set_timeout(d, "p:m", 900)
    assert mt.resolve_timeout(d, "p:m") == 900.0           # per-ref beats global
    assert mt.resolve_timeout(d, "other") == 120.0         # other model → global
    mt.set_timeout(d, "p:m", 0)                            # 0 = off
    assert mt.resolve_timeout(d, "p:m") == 0.0
    mt.reset(d, "p:m")
    assert mt.resolve_timeout(d, "p:m") == 120.0           # back to global


def test_config_request_timeout_is_the_fallback(tmp_path):
    cfg = {"model": {"request_timeout": 45}}
    assert mt.resolve_timeout(str(tmp_path), "p:m", cfg) == 45.0
    mt.set_timeout(str(tmp_path), mt.GLOBAL, 200, glob=True)
    assert mt.resolve_timeout(str(tmp_path), "p:m", cfg) == 200.0  # store beats config


def test_missing_file_is_none(tmp_path):
    assert mt.load_state(str(tmp_path)) == {}
    assert mt.resolve_timeout(str(tmp_path), "p:m") is None


# ---- cli helpers ---------------------------------------------------------------


def test_parse_duration_and_format():
    from chipchamp.cli import _fmt_secs, _parse_duration
    assert _parse_duration("1200") == 1200.0
    assert _parse_duration("20m") == 1200.0
    assert _parse_duration("300s") == 300.0
    assert _parse_duration("off") == 0.0 and _parse_duration("none") == 0.0
    assert _parse_duration("nonsense") is None
    assert _fmt_secs(900) == "15m (900s)"
    assert _fmt_secs(0) == "off (no wall-clock limit)"
    assert _fmt_secs(None) == "provider default"
    assert _fmt_secs(45) == "45s"


# ---- streaming keep-alive (content-liveness) -----------------------------------


def test_streaming_stall_trips_but_live_stream_survives(monkeypatch):
    from chipchamp.agent.providers import openai_compat as oc

    # a live stream (tokens arriving) must complete even with a small budget
    def live():
        yield b': keepalive\n'
        yield b'data: {"choices":[{"delta":{"content":"hel"}}]}\n'
        yield b'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
        yield b'data: [DONE]\n'
    out = oc.assemble_sse(live(), lambda ch, t: None, idle_timeout=999)
    assert out["choices"][0]["message"]["content"] == "hello"

    # a stalled stream (keepalives, no tokens) must raise once the gap is exceeded
    seq = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr(oc.time, "monotonic", lambda: next(seq))

    def stalled():
        yield b': keepalive\n'          # gap 0→100 > 50 → trip on this line
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
    with pytest.raises(TimeoutError):
        oc.assemble_sse(stalled(), lambda ch, t: None, idle_timeout=50)


def test_gateway_streaming_skips_total_deadline_but_blocking_enforces_it():
    from chipchamp.agent.gateway import ModelGateway, ModelTimeout
    from chipchamp.agent.providers.base import ModelResponse

    class StreamProv:  # declares on_delta → "streaming"
        name = "lmstudio"
        request_timeout = None

        def available(self):
            return True

        def chat(self, model, system, transcript, tools, on_delta=None, **kw):
            time.sleep(0.06)                 # > the 0.01s budget below
            return ModelResponse(text="streamed")

    gw = ModelGateway(StreamProv(), "m", timeout=0.01)
    r = gw.complete("s", [{"role": "user", "content": "x"}], [],
                    on_delta=lambda ch, c: None)
    assert r.text == "streamed"             # a live stream is NOT killed

    class BlockProv:  # no on_delta → non-streaming → total deadline applies
        name = "anthropic"
        request_timeout = None

        def available(self):
            return True

        def chat(self, model, system, transcript, tools, **kw):
            time.sleep(0.06)
            return ModelResponse(text="late")

    gwb = ModelGateway(BlockProv(), "m", timeout=0.01)
    with pytest.raises(ModelTimeout):
        gwb.complete("s", [{"role": "user", "content": "x"}], [])


# ---- loop surfaces a retryable timeout -----------------------------------------


def test_loop_flags_timeout_for_interactive_retry(ctx):
    from chipchamp.agent.loop import AgentLoop

    class TimeoutGateway:
        available = True
        model = "slow"
        ref = "ollama:laguna-s-2.1:latest"
        timeout = 300.0
        provider = None

        def complete(self, system, transcript, tools, audit_items=None,
                     on_delta=None):
            raise TimeoutError("model call exceeded 300s")

    out = AgentLoop(ctx, TimeoutGateway()).run("do something slow")
    assert out["timed_out"] is True
    assert out["ref"] == "ollama:laguna-s-2.1:latest"
    assert out["timeout"] == 300.0
