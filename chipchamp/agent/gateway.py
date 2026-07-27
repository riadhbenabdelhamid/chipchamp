"""Model gateway (SPEC §14): provider-agnostic access + the context audit log.

A thin facade over a :class:`~chipchamp.agent.providers.Provider` (Anthropic,
OpenAI, or any OpenAI-compatible local server). Every model call appends to the
**context audit log** (FR-SEC-03): timestamp, provider+model, token usage and the
provenance of the context sent — the record a security team reviews. An offline
:class:`NullGateway` lets the platform run scripted playbooks with no model.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .providers.base import DEFAULT_REQUEST_TIMEOUT, ModelResponse, Provider


class ModelTimeout(TimeoutError):
    """A model call exceeded its total wall-clock budget."""


def _call_with_deadline(fn: Callable[[], object], timeout: float):
    """Run `fn` with a hard total-time budget. urllib's socket timeout only
    bounds individual reads (a streaming/keepalive provider resets it), so a slow
    or wedged endpoint can otherwise block forever. We run the call in a daemon
    thread and abandon it on timeout — the orphaned request dies with the process
    (or completes and is discarded)."""
    if not timeout or timeout <= 0:
        return fn()
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # propagate provider errors to the caller
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise ModelTimeout(f"model call exceeded {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class ContextAuditLog:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, model: str, provider: str, items: list[dict], usage: dict) -> None:
        entry = {"ts": time.time(), "provider": provider, "model": model,
                 "usage": usage, "context_items": items}
        with open(self.path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


class ModelGateway:
    def __init__(self, provider: Provider, model: str,
                 audit_log: Optional[str] = None, max_tokens: int = 16384,
                 timeout: Optional[float] = None, reasoning_effort: str = "",
                 options: Optional[dict] = None):
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        # `/effort` retunes this on the live gateway — kept a plain attribute so
        # a running loop picks it up on its next call without a rebuild.
        self.reasoning_effort = reasoning_effort or ""
        # `/model tune` generation options (temperature/top_p/num_ctx/…), sent
        # verbatim in the chat request. Same live-attribute contract as effort:
        # retuned in place so a running loop uses it on the next call.
        self.options = dict(options or {})
        self.timeout = DEFAULT_REQUEST_TIMEOUT if timeout is None else float(timeout)
        # Thread THIS gateway's budget down to the provider's HTTP layer: the
        # wall-clock deadline and the socket timeout must be one budget even
        # when it comes from config [model] request_timeout, not just the env
        # default — a lower socket would silently cap the deadline (the exact
        # bug the shared default fixed for the env path).
        if provider is not None:
            provider.request_timeout = self.timeout
        self.audit = ContextAuditLog(audit_log) if audit_log else None

    @property
    def available(self) -> bool:
        try:
            return self.provider.available()
        except Exception:
            return False

    def _chat_streams(self) -> bool:
        """Does this provider's chat() accept an on_delta callback?"""
        cached = getattr(self, "_streams", None)
        if cached is None:
            import inspect
            try:
                cached = "on_delta" in inspect.signature(
                    self.provider.chat).parameters
            except (TypeError, ValueError):
                cached = False
            self._streams = cached
        return cached

    @property
    def ref(self) -> str:
        return f"{self.provider.name}:{self.model}"

    def complete(self, system: str, transcript: list[dict], tools: list[dict],
                 audit_items: Optional[list[dict]] = None,
                 on_delta: Optional[Callable] = None) -> ModelResponse:
        # Pass reasoning_effort only when set, so an unset effort is identical
        # to the pre-feature call — a Provider that predates the parameter (or
        # never needs it) keeps working untouched. When it IS set, a provider
        # that can't accept it should fail loudly rather than silently ignore.
        kw = {"max_tokens": self.max_tokens}
        if self.reasoning_effort:
            kw["reasoning_effort"] = self.reasoning_effort
        # Same guard as effort: only pass options when set, so an untuned call
        # stays wire-identical to the pre-feature behavior for any provider.
        if self.options:
            kw["options"] = self.options
        # Streaming is opt-in per provider: only pass on_delta to a chat()
        # that declares it, so providers that predate streaming (anthropic,
        # test fakes) keep their exact signature and behavior.
        streaming = on_delta is not None and self._chat_streams()
        if streaming:
            kw["on_delta"] = on_delta
        call = lambda: self.provider.chat(self.model, system, transcript, tools, **kw)
        if streaming:
            # A streamed call carries its OWN content-liveness timeout (the SSE
            # loop aborts if no token arrives for `self.timeout`), so a total
            # wall-clock deadline here would needlessly kill a model that is slow
            # but still emitting — exactly the "too slow to be interactive" case.
            # Let it run as long as tokens keep flowing; a stalled stream still
            # raises TimeoutError from inside.
            resp = call()
        else:
            # No liveness signal (non-streaming provider / no on_delta): the
            # total wall-clock budget is the only guard against a wedged call.
            resp = _call_with_deadline(call, self.timeout)
        if self.audit:
            self.audit.record(self.model, self.provider.name, audit_items or [],
                              {"input_tokens": resp.input_tokens,
                               "output_tokens": resp.output_tokens})
        return resp


class NullGateway:
    """Offline gateway — the platform runs scripted playbooks without a model
    (air-gapped bring-up D4, and deterministic tests)."""

    available = True
    model = "offline"
    ref = "offline:none"

    def complete(self, *a, **k) -> ModelResponse:
        return ModelResponse(text="[offline: no model configured; use a scripted "
                             "playbook such as `chipchamp triage` or `chipchamp lint`]",
                             stop_reason="end_turn")
