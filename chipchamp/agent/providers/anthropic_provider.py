"""Anthropic provider (SPEC §14). Uses the anthropic SDK when installed; degrades
to unavailable (with a clear message) otherwise. Model discovery uses the SDK's
models list, falling back to a static set of known ids."""
from __future__ import annotations

from .base import ModelInfo, ModelResponse, Provider

_KNOWN = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5",
          "claude-haiku-4-5-20251001"]


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t.get("schema") or {"type": "object", "properties": {}}}
            for t in tools]


def to_anthropic_messages(transcript: list[dict]) -> list[dict]:
    msgs: list[dict] = []
    for turn in transcript:
        role = turn["role"]
        if role == "user":
            msgs.append({"role": "user", "content": turn.get("content", "")})
        elif role == "assistant":
            blocks = []
            if turn.get("content"):
                blocks.append({"type": "text", "text": turn["content"]})
            for tc in turn.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"],
                               "name": tc["name"], "input": tc.get("input") or {}})
            msgs.append({"role": "assistant", "content": blocks or turn.get("content", "")})
        elif role == "tool":
            content = [{"type": "tool_result", "tool_use_id": res["id"],
                        "content": res.get("output", "")} for res in turn.get("content", [])]
            msgs.append({"role": "user", "content": content})
    return msgs


class AnthropicProvider(Provider):
    def _client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.config.resolved_key(),
                                   base_url=self.config.base_url or None)

    def available(self) -> bool:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return bool(self.config.resolved_key())

    def list_models(self) -> list[ModelInfo]:
        try:
            resp = self._client().models.list(limit=50)
            ids = [m.id for m in resp.data]
        except Exception:
            ids = _KNOWN
        return [ModelInfo(id=i, provider=self.name) for i in ids]

    def chat(self, model, system, transcript, tools, max_tokens=4096,
             reasoning_effort="", options=None) -> ModelResponse:
        # Anthropic controls thinking via a token budget, not an effort ladder —
        # accepted for interface parity and ignored (see agent/effort.py).
        del reasoning_effort
        # `/model tune` — apply only the sampling knobs the Messages API accepts;
        # local-server-specific keys (num_ctx, …) are silently dropped here.
        kw = {k: options[k] for k in ("temperature", "top_p", "top_k",
                                      "stop_sequences")
              if options and k in options}
        resp = self._client().messages.create(
            model=model, max_tokens=max_tokens, system=system or "",
            tools=to_anthropic_tools(tools),
            messages=to_anthropic_messages(transcript), **kw)
        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name,
                                   "input": block.input})
        return ModelResponse(
            text="\n".join(text_parts), tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens)
