"""OpenAI-compatible provider (SPEC §14, FR-MDL-01).

Covers OpenAI itself and every server that speaks its ``/v1/chat/completions`` and
``/v1/models`` API — LM Studio, Ollama, vLLM, llama.cpp, LocalAI, Azure OpenAI,
and cloud gateways. Implemented on the standard library (urllib) so it adds no
dependency and works in an air-gapped install.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import (DEFAULT_REQUEST_TIMEOUT, ModelInfo, ModelResponse, Provider)

# OpenAI's reasoning models (o-series, gpt-5) reject "max_tokens" and require
# "max_completion_tokens"; every other OpenAI-compatible server (LM Studio,
# vLLM, Ollama — gpt-oss included) takes "max_tokens". Family test mirrors the
# boundary-anchored patterns in agent/effort.py.
_NEEDS_MAX_COMPLETION = re.compile(r"(?:^|[:/])(?:o[1345](?:$|[-:.])|gpt-5(?:$|[-.:]))")


def _token_param(model: str) -> str:
    return ("max_completion_tokens"
            if _NEEDS_MAX_COMPLETION.search((model or "").lower())
            else "max_tokens")


# `/model tune` options are merged into the chat payload verbatim, EXCEPT these
# request-shaping fields (the caller owns them; a tune value clobbering them
# would corrupt the request). Everything else — temperature, top_p, top_k,
# repeat_penalty, seed, num_ctx, num_predict, and any server extension — passes
# straight through to whatever the endpoint accepts.
_WIRE_RESERVED = {"model", "messages", "tools", "stream", "stream_options"}


def _apply_options(payload: dict, options: Optional[dict]) -> None:
    for k, v in (options or {}).items():
        if k not in _WIRE_RESERVED:
            payload[k] = v


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t.get("schema") or {"type": "object", "properties": {}}}}
            for t in tools]


# Strict chat templates (Mistral family) require tool-call ids of EXACTLY nine
# alphanumerics and reject them otherwise. Ids are opaque echoes, so normalize
# deterministically — the assistant call and its result derive the same wire id
# from the same original, staying paired. Benign for every other server.
_WIRE_ID_OK = re.compile(r"^[a-zA-Z0-9]{9}$")


def _wire_id(raw: str) -> str:
    raw = raw or ""
    if _WIRE_ID_OK.match(raw):
        return raw
    return hashlib.sha1(raw.encode()).hexdigest()[:9]


def to_openai_messages(system: str, transcript: list[dict]) -> list[dict]:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for turn in transcript:
        role = turn["role"]
        if role == "user":
            m = {"role": "user", "content": turn.get("content", "")}
        elif role == "assistant":
            m = {"role": "assistant", "content": turn.get("content") or ""}
            if turn.get("tool_calls"):
                m["tool_calls"] = [
                    {"id": _wire_id(tc["id"]), "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": json.dumps(tc.get("input") or {})}}
                    for tc in turn["tool_calls"]]
                # OpenAI allows empty content alongside tool_calls
                if not m["content"]:
                    m["content"] = None
        elif role == "tool":
            for res in turn.get("content", []):
                msgs.append({"role": "tool", "tool_call_id": _wire_id(res["id"]),
                             "content": res.get("output", "")})
            continue
        else:
            continue
        # Strict templates also demand user/assistant alternation. Adjacent
        # same-role text turns (e.g. a model narrating across several turns
        # after the nudges cap out) are template-invalid — coalesce them.
        prev = msgs[-1] if msgs else None
        if (prev is not None and prev["role"] == m["role"]
                and m["role"] in ("user", "assistant")
                and not prev.get("tool_calls") and not m.get("tool_calls")):
            prev["content"] = ((prev.get("content") or "") + "\n\n"
                               + (m.get("content") or "")).strip()
        else:
            msgs.append(m)
    return msgs


def parse_openai_response(data: dict) -> ModelResponse:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    text = msg.get("content") or ""
    # Reasoning models: LM Studio/vLLM split chain-of-thought into
    # reasoning_content (or reasoning); some templates inline <think>…</think>
    # in content instead. Either way the transcript must carry only the
    # actionable text — reasoning is surfaced separately (and a turn that is
    # ONLY reasoning must not read as an empty end-of-turn upstream).
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if "</think>" in text:
        head, _, tail = text.partition("</think>")
        reasoning = (reasoning + "\n" + head.replace("<think>", "")).strip()
        text = tail.lstrip("\n")
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""),
                           "input": args})
    usage = data.get("usage", {}) or {}
    return ModelResponse(
        text=text, tool_calls=tool_calls,
        stop_reason=choice.get("finish_reason", ""),
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        reasoning=reasoning)


def assemble_sse(fh, on_delta, idle_timeout: Optional[float] = None) -> dict:
    """Fold an OpenAI-style SSE chat stream back into the one-shot response
    shape ``parse_openai_response`` already understands, so streaming and
    non-streaming share a single parser (one place for the <think> split,
    tool-call decode and usage handling).

    ``fh`` yields raw bytes lines (an http response). ``on_delta`` is called
    live as ``on_delta(channel, text)`` with channel "text" or "reasoning" —
    and ``("tool", name)`` once per tool call as its name first appears, so a
    UI can show *what is being composed* while arguments stream in.

    ``idle_timeout`` (seconds) is a **content-liveness** budget: if no real
    token (text/reasoning/tool) arrives for that long, the stream is considered
    stalled and aborts with TimeoutError. Keepalive comments reset nothing —
    only genuine output does — so a slow-but-progressing model runs as long as
    it keeps producing, while a wedged endpoint still trips."""
    msg: dict = {"content": "", "reasoning_content": ""}
    calls: dict[int, dict] = {}   # index -> {id, function: {name, arguments}}
    finish = ""
    usage: dict = {}
    last = time.monotonic()       # time of the last genuine token
    for raw in fh:
        # Checked on every inbound line (keepalives included), so a server that
        # holds the socket open with comments but emits nothing still trips.
        if idle_timeout and time.monotonic() - last > idle_timeout:
            raise TimeoutError(
                f"stream stalled: no output for {idle_timeout:.0f}s")
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue  # comments / event: lines / keepalives
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue  # a torn frame must not kill the whole call
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]  # final chunk (stream_options include_usage)
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                msg["content"] += piece
                on_delta("text", piece)
                last = time.monotonic()
            reason = delta.get("reasoning_content") or delta.get("reasoning")
            if reason:
                msg["reasoning_content"] += reason
                on_delta("reasoning", reason)
                last = time.monotonic()
            for tc in delta.get("tool_calls") or []:
                last = time.monotonic()
                idx = tc.get("index", 0)
                slot = calls.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                    on_delta("tool", slot["function"]["name"])
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    if not usage:
        # Servers that omit stream usage (no include_usage support) would
        # otherwise report 0 tokens and silently starve the budget ledger —
        # estimate from text so the ledger keeps moving (~4 chars/token).
        est = (len(msg["content"]) + len(msg["reasoning_content"])) // 4
        usage = {"prompt_tokens": 0, "completion_tokens": est}
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": usage}


class OpenAICompatProvider(Provider):
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        key = self.config.resolved_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        h.update(self.config.extra_headers)
        return h

    def _get(self, path: str, timeout: float = 4.0):
        url = self.config.base_url.rstrip("/") + path
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def _budget(self, timeout: Optional[float]) -> Optional[float]:
        # The socket budget, largest scope wins: explicit arg > the owning
        # gateway's per-call budget (Provider.request_timeout, carries config
        # [model] request_timeout; 0 means no limit) > the shared env default.
        # Never a hardcoded ceiling: a non-streaming endpoint sends nothing
        # while the model thinks, so THIS is what a slow reasoning model hits.
        if timeout is not None:
            return timeout
        rt = self.request_timeout
        return (rt if rt else None) if rt is not None else DEFAULT_REQUEST_TIMEOUT

    def _request(self, path: str, payload: dict):
        url = self.config.base_url.rstrip("/") + path
        body = json.dumps(payload).encode()
        return urllib.request.Request(url, data=body, headers=self._headers(),
                                      method="POST")

    def _post(self, path: str, payload: dict, timeout: Optional[float] = None):
        req = self._request(path, payload)
        with urllib.request.urlopen(req, timeout=self._budget(timeout)) as r:
            return json.load(r)

    def _post_stream(self, path: str, payload: dict, on_delta,
                     timeout: Optional[float] = None) -> dict:
        """POST with ``stream: true`` and assemble the SSE chunks back into a
        complete (non-streaming-shaped) response dict, invoking ``on_delta``
        live for each text/reasoning fragment. The socket timeout applies per
        read — for a stream that means *time between chunks*, so a wedged
        server still trips it while a long, actively-generating answer does
        not die between tokens."""
        req = self._request(path, payload)
        budget = self._budget(timeout)
        with urllib.request.urlopen(req, timeout=budget) as r:
            # the socket timeout bounds a fully-silent connection; the same
            # budget bounds a keepalive-but-no-tokens stall (content-liveness).
            return assemble_sse(r, on_delta, idle_timeout=budget)

    def available(self) -> bool:
        # remote providers need a key; local ones just need to be reachable
        if not self.config.local and not self.config.resolved_key():
            return False
        try:
            self._get("/models", timeout=2.5)
            return True
        except Exception:
            # a remote provider with a key may block /models but still chat
            return bool(self.config.resolved_key()) and not self.config.local

    def list_models(self) -> list[ModelInfo]:
        try:
            data = self._get("/models")
        except Exception:
            return []
        out = []
        for m in data.get("data", data if isinstance(data, list) else []):
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if mid:
                out.append(ModelInfo(id=mid, provider=self.name))
        return sorted(out, key=lambda x: x.id)

    def chat(self, model, system, transcript, tools, max_tokens=4096,
             reasoning_effort="", options=None, on_delta=None) -> ModelResponse:
        payload = {"model": model,
                   "messages": to_openai_messages(system, transcript),
                   _token_param(model): max_tokens}
        # Only send when set: a non-reasoning model/server may reject an
        # unknown field, so an unset effort must be wire-identical to before.
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if tools:
            payload["tools"] = to_openai_tools(tools)
        # `/model tune` overrides last, so a tuned value wins over a default but
        # never over the request-shaping fields in _WIRE_RESERVED.
        _apply_options(payload, options)
        if on_delta is not None:
            # Live streaming: tokens render as they generate. Any HTTP error
            # falls back to the plain call — a server that rejects streaming
            # (or its stream_options) must degrade to working, not to dead;
            # a genuinely broken request then fails with the REAL error below.
            stream_payload = dict(payload)
            stream_payload["stream"] = True
            stream_payload["stream_options"] = {"include_usage": True}
            try:
                data = self._post_stream("/chat/completions", stream_payload,
                                         on_delta)
                return parse_openai_response(data)
            except urllib.error.HTTPError:
                pass
        try:
            data = self._post("/chat/completions", payload)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code} from {self.name}: {detail}") from e
        return parse_openai_response(data)
