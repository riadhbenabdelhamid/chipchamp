"""Retry decisions come from exception type and HTTP status, not from
substrings of str(e) that never matched the real messages."""
from __future__ import annotations

import http.client
import json
import urllib.error
from email.message import Message

from chipchamp.agent import retry
from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse


def _http(code, detail, headers=None):
    """The shape openai_compat raises: RuntimeError chained from HTTPError."""
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    cause = urllib.error.HTTPError("http://x", code, "err", hdrs, None)
    try:
        raise RuntimeError(f"HTTP {code} from lmstudio: {detail}") from cause
    except RuntimeError as e:
        return e


def test_server_and_rate_limit_errors_retry():
    for code in (500, 502, 503, 529, 429, 408):
        ok, why = retry.classify(_http(code, "x"))
        assert ok, code
        assert str(code) in why


def test_auth_and_not_found_never_retry():
    for code in (401, 403, 404):
        assert retry.classify(_http(code, "x"))[0] is False, code


def test_400_only_retries_when_it_is_a_sampling_glitch():
    glitch = _http(400, "The model produced output that does not match the "
                        "expected peg-native format")
    assert retry.classify(glitch) == (True, "HTTP 400 sampling glitch")
    for detail in ("failed to load model", "context length exceeded",
                   "tool_use ids were found without tool_result blocks"):
        ok, why = retry.classify(_http(400, detail))
        assert ok is False and "deterministic" in why, detail


def test_real_transient_exceptions_retry():
    for e in (http.client.RemoteDisconnected("Remote end closed connection without response"),
              http.client.IncompleteRead(b""),
              ConnectionResetError(104, "Connection reset by peer"),
              urllib.error.URLError("timed out"),
              json.JSONDecodeError("Expecting value", "", 0)):
        assert retry.classify(e)[0], type(e).__name__


def test_sdk_style_errors_use_status_code_and_class_name():
    assert retry.classify(type("InternalServerError", (Exception,), {"status_code": 529})("Error code: 529"))[0]
    assert retry.classify(type("APIConnectionError", (Exception,), {})("Connection error."))[0]
    assert retry.classify(type("BadRequestError", (Exception,), {"status_code": 400})("Error code: 400 - prompt too long"))[0] is False


def test_timeouts_and_unknown_errors_are_not_retried():
    assert retry.classify(TimeoutError("model call exceeded 60s")) == (False, "timeout")
    assert retry.classify(RuntimeError("something odd"))[0] is False


def test_backoff_honours_retry_after_else_jitters():
    assert retry.backoff(_http(429, "slow down", {"Retry-After": "7"}), 0) == 7.0
    waits = {round(retry.backoff(_http(503, "x"), 1), 3) for _ in range(20)}
    assert all(3.0 <= w <= 4.5 for w in waits) and len(waits) > 1


def test_loop_retries_transient_and_stops_on_deterministic(ctx, monkeypatch):
    slept = []
    import time
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    class Gw:
        available, model, ref = True, "fake", "fake:test"

        def __init__(self, errors, then):
            self.errors, self.then, self.n = list(errors), then, 0

        def complete(self, system, transcript, tools, audit_items=None):
            self.n += 1
            if self.errors:
                raise self.errors.pop(0)
            return self.then

    ok = ModelResponse(text="fine", tool_calls=[])
    gw = Gw([http.client.RemoteDisconnected("gone"), _http(529, "overloaded")], ok)
    assert AgentLoop(ctx, gw)._call_with_retry("S", [{"role": "user", "content": "x"}]).text == "fine"
    assert gw.n == 3 and len(slept) == 2

    gw = Gw([_http(400, "failed to load model")], ok)
    try:
        AgentLoop(ctx, gw)._call_with_retry("S", [{"role": "user", "content": "x"}])
        assert False, "deterministic 400 must not be retried"
    except RuntimeError:
        assert gw.n == 1
