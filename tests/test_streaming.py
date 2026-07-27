"""Tier-1 liveness: SSE token streaming (provider → gateway → loop events)
and the job runner's live log for tailing running EDA jobs."""
from __future__ import annotations

import io
import json
import urllib.error

from chipchamp.agent.providers.base import ProviderConfig
from chipchamp.agent.providers.openai_compat import (
    OpenAICompatProvider, assemble_sse, parse_openai_response)


def _sse(*events) -> list[bytes]:
    """Encode chunk dicts (or raw strings) as SSE byte lines."""
    out = []
    for e in events:
        data = e if isinstance(e, str) else json.dumps(e)
        out.append(f"data: {data}\n".encode())
        out.append(b"\n")
    return out


def _delta(**kw) -> dict:
    return {"choices": [{"index": 0, "delta": kw, "finish_reason": None}]}


def test_assemble_sse_text_reasoning_and_tools():
    stream = _sse(
        _delta(role="assistant", reasoning_content="let me "),
        _delta(reasoning_content="think"),
        _delta(content="Half "),
        _delta(content="done."),
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_abc", "type": "function",
             "function": {"name": "fs.write", "arguments": "{\"pa"}}]},
            "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "th\": \"a.sv\"}"}}]},
            "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        "[DONE]",
    )
    deltas = []
    data = assemble_sse(iter(stream), lambda ch, tx: deltas.append((ch, tx)))
    resp = parse_openai_response(data)
    assert resp.text == "Half done."
    assert resp.reasoning == "let me think"
    assert resp.tool_calls == [{"id": "call_abc", "name": "fs.write",
                                "input": {"path": "a.sv"}}]
    assert resp.stop_reason == "tool_calls"
    assert resp.input_tokens == 11 and resp.output_tokens == 7
    # live callbacks arrived in generation order, tool name announced once
    assert deltas == [("reasoning", "let me "), ("reasoning", "think"),
                      ("text", "Half "), ("text", "done."),
                      ("tool", "fs.write")]


def test_assemble_sse_estimates_missing_usage():
    stream = _sse(_delta(content="x" * 40), "[DONE]")
    data = assemble_sse(iter(stream), lambda ch, tx: None)
    assert data["usage"]["completion_tokens"] == 10  # ~4 chars/token


def test_assemble_sse_survives_torn_frames_and_keepalives():
    stream = [b": keepalive\n", b"event: ping\n",
              b'data: {"choices": [{"delta": {"content": "ok"}, '
              b'"finish_reason": null}]}\n',
              b"data: {broken json\n",
              b"data: [DONE]\n"]
    data = assemble_sse(iter(stream), lambda ch, tx: None)
    assert parse_openai_response(data).text == "ok"


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(ProviderConfig(
        name="lmstudio", kind="openai", base_url="http://test", api_key="k",
        local=True))


def test_chat_falls_back_when_server_rejects_streaming():
    """A server that 400s on stream/stream_options must degrade to the plain
    call, not fail the task."""
    p = _provider()
    calls = {"stream": 0, "plain": 0}

    def post_stream(path, payload, on_delta, timeout=None):
        calls["stream"] += 1
        raise urllib.error.HTTPError("http://test", 400, "no stream", {},
                                     io.BytesIO(b"unsupported"))

    def post(path, payload, timeout=None):
        calls["plain"] += 1
        assert "stream" not in payload  # the fallback must be wire-clean
        return {"choices": [{"message": {"content": "plain"},
                             "finish_reason": "stop"}], "usage": {}}

    p._post_stream = post_stream
    p._post = post
    resp = p.chat("m", "sys", [{"role": "user", "content": "hi"}], [],
                  on_delta=lambda ch, tx: None)
    assert resp.text == "plain"
    assert calls == {"stream": 1, "plain": 1}


def test_chat_without_on_delta_never_streams():
    p = _provider()

    def post_stream(*a, **k):
        raise AssertionError("must not stream without a consumer")

    p._post_stream = post_stream
    p._post = lambda path, payload, timeout=None: {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {}}
    assert p.chat("m", "s", [{"role": "user", "content": "q"}], []).text == "hi"


def test_gateway_gates_on_delta_by_provider_signature(tmp_path):
    """on_delta reaches a streaming-capable provider and is withheld from one
    with the pre-streaming signature — no TypeError either way."""
    from chipchamp.agent.gateway import ModelGateway
    from chipchamp.agent.providers.base import ModelResponse

    class OldProvider:
        name = "old"
        request_timeout = None

        def chat(self, model, system, transcript, tools, max_tokens=4096,
                 reasoning_effort=""):
            return ModelResponse(text="old-style")

    class NewProvider(OldProvider):
        name = "new"

        def chat(self, model, system, transcript, tools, max_tokens=4096,
                 reasoning_effort="", on_delta=None):
            if on_delta:
                on_delta("text", "streamed")
            return ModelResponse(text="new-style")

    got = []
    gw_old = ModelGateway(OldProvider(), "m")
    assert gw_old.complete("s", [], [], on_delta=lambda c, t: got.append(t)) \
        .text == "old-style"
    assert got == []
    gw_new = ModelGateway(NewProvider(), "m")
    assert gw_new.complete("s", [], [], on_delta=lambda c, t: got.append(t)) \
        .text == "new-style"
    assert got == ["streamed"]


def test_loop_surfaces_deltas_as_events(ctx):
    from chipchamp.agent.loop import AgentLoop
    from chipchamp.agent.providers.base import ModelResponse

    class StreamingGateway:
        available = True
        model = "fake"
        ref = "fake:stream"

        def complete(self, system, transcript, tools, audit_items=None,
                     on_delta=None):
            if on_delta:
                on_delta("reasoning", "hmm ")
                on_delta("text", "final ")
                on_delta("text", "answer.")
            return ModelResponse(text="final answer.", stop_reason="end_turn")

    events = []
    out = AgentLoop(ctx, StreamingGateway(),
                    on_event=lambda k, d: events.append((k, d))).run("q")
    assert out["text"] == "final answer."
    chunks = [(d["channel"], d["chunk"]) for k, d in events if k == "delta"]
    assert chunks == [("reasoning", "hmm "), ("text", "final "),
                      ("text", "answer.")]


def test_runner_streams_live_log(tmp_path):
    """Step output must land in <job>/live.log AS the process runs (so a UI
    can tail it), while the persisted step log keeps its exact format."""
    from chipchamp.adapters.base import Step
    from chipchamp.adapters.registry import AdapterRegistry
    from chipchamp.jobs.runner import JobRunner

    runner = JobRunner(str(tmp_path / "runs"), AdapterRegistry())
    seen = []
    runner.on_step = lambda job_id, live: seen.append((job_id, live))
    job_dir = tmp_path / "runs" / "J-TEST"
    job_dir.mkdir(parents=True)
    st = Step(argv=["bash", "-c", "echo out-line; echo err-line >&2"],
              cwd=str(tmp_path))
    sr = runner._run_step(st, job_dir, 0, timeout=30)
    assert sr.rc == 0
    assert "out-line" in sr.stdout and "err-line" in sr.stderr
    live = (job_dir / "live.log").read_text()
    assert "out-line" in live and "err-line" in live
    assert seen and seen[0][0] == "J-TEST" and seen[0][1].endswith("live.log")
    # the persisted per-step log keeps the pre-streaming separated format
    step_log = (job_dir / "step0.log").read_text()
    assert "--- stdout ---" in step_log and "--- stderr ---" in step_log
    assert "out-line" in step_log and "err-line" in step_log


def test_runner_step_survives_missing_binary(tmp_path):
    from chipchamp.adapters.base import Step
    from chipchamp.adapters.registry import AdapterRegistry
    from chipchamp.jobs.runner import JobRunner

    runner = JobRunner(str(tmp_path / "runs"), AdapterRegistry())
    job_dir = tmp_path / "runs" / "J-MISS"
    job_dir.mkdir(parents=True)
    sr = runner._run_step(Step(argv=["definitely-not-a-binary-xyz"],
                               cwd=str(tmp_path)), job_dir, 0, timeout=5)
    assert sr.rc == 127 and "Error" in sr.stderr


def test_close_fences_balances_streaming_prefix():
    from chipchamp.ui import _close_fences
    assert _close_fences("a\n```sv\nmodule m;") == "a\n```sv\nmodule m;\n```"
    assert _close_fences("a\n```sv\ncode\n```\nb") == "a\n```sv\ncode\n```\nb"


def test_stream_view_and_job_tail_are_headless_safe():
    """Off a TTY (the test env) both renderers must be inert no-ops."""
    from chipchamp.ui import JobTail, StreamView
    sv = StreamView("m")
    sv.start()
    sv.feed("text", "hello")   # ignored: never started a thread
    sv.stop()
    assert sv.streamed_chars == 0
    jt = JobTail("sim")
    jt.start()
    jt.attach("/nonexistent/live.log")
    jt.stop()  # must not raise
