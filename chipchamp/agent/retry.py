"""Which model-call failures are worth retrying, and how long to wait.

The old test was `any(k in str(e) for k in (...))` over a list of
substrings. It did not match what the real exceptions say —
`str(JSONDecodeError)` is "Expecting value: line 1 column 1", a dropped
keep-alive is "Remote end closed connection without response", a DNS blip
is "<urlopen error ...>", Anthropic's overload is "Error code: 529" — so one
transient hiccup ended a whole run. Meanwhile every HTTP 400 was retried
three times with sleeps, including the deterministic ones: a context
overflow, a model that failed to load, a transcript the API rejects.

Classify by type and status code first; fall back to the message only for
errors that carry nothing else.
"""
from __future__ import annotations

import http.client
import json
import random
import re
import urllib.error

RETRY_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
NEVER_CODES = {401, 403, 404, 413, 422}

# A 400 that is a sampling glitch, not a bad request: LM Studio's
# grammar-constrained decoder rejecting one malformed sample.
_GLITCH_400 = ("peg-native", "expected .* format", "produced output",
               "grammar", "malformed sample")
# Message-only fallback for errors wrapped as plain RuntimeError.
_TRANSIENT_WORDS = ("connection reset", "connection refused", "remote end closed",
                    "incomplete read", "temporarily unavailable", "timed out",
                    "overloaded", "rate limit")

_CODE_RX = re.compile(r"\b(?:HTTP|Error code:?)\s*(\d{3})\b", re.I)


def status_code(e: BaseException) -> int | None:
    """The HTTP status an exception carries, however it is wrapped."""
    for obj in (e, getattr(e, "__cause__", None)):
        if obj is None:
            continue
        for attr in ("status_code", "code", "status"):
            v = getattr(obj, attr, None)
            if isinstance(v, int) and 100 <= v <= 599:
                return v
    m = _CODE_RX.search(str(e))
    return int(m.group(1)) if m else None


def classify(e: BaseException) -> tuple[bool, str]:
    """-> (retryable, reason). Timeouts are the caller's business, not ours."""
    if isinstance(e, TimeoutError):
        return False, "timeout"
    cause = getattr(e, "__cause__", None)
    for obj in (e, cause):
        if isinstance(obj, (ConnectionError, http.client.HTTPException,
                            urllib.error.URLError, json.JSONDecodeError)) \
                and not isinstance(obj, urllib.error.HTTPError):
            return True, type(obj).__name__
        if obj is not None and "Connection" in type(obj).__name__:
            return True, type(obj).__name__          # SDK APIConnectionError etc.
    code = status_code(e)
    if code in NEVER_CODES:
        return False, f"HTTP {code}"
    if code in RETRY_CODES:
        return True, f"HTTP {code}"
    if code == 400:
        s = str(e).lower()
        if any(re.search(p, s) for p in _GLITCH_400):
            return True, "HTTP 400 sampling glitch"
        return False, "HTTP 400 (deterministic)"
    if code is not None and 400 <= code < 500:
        return False, f"HTTP {code}"
    s = str(e).lower()
    if any(w in s for w in _TRANSIENT_WORDS):
        return True, "transient (message)"
    return False, type(e).__name__


def retry_after(e: BaseException) -> float | None:
    """Honour a Retry-After header when the exception carries one."""
    for obj in (e, getattr(e, "__cause__", None)):
        hdrs = getattr(obj, "headers", None)
        if hdrs is None:
            resp = getattr(obj, "response", None)
            hdrs = getattr(resp, "headers", None)
        if hdrs is None:
            continue
        try:
            v = hdrs.get("Retry-After") if hasattr(hdrs, "get") else None
        except Exception:
            v = None
        if v:
            try:
                return min(60.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                return None
    return None


def backoff(e: BaseException, attempt: int, base: float = 1.5) -> float:
    """Seconds to wait before attempt+1: Retry-After if given, else a jittered
    linear back-off (two clients retrying in lock-step is how a recovering
    server gets knocked over again)."""
    ra = retry_after(e)
    if ra is not None:
        return ra
    return base * (attempt + 1) * (1.0 + 0.5 * random.random())
