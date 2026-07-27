"""Per-model generation/runtime options (`/model tune`) — SPEC §14.

The reasoning-effort lever (:mod:`chipchamp.agent.effort`) exposes ONE knob; this
exposes the rest. A local server (LM Studio, Ollama) accepts many generation
parameters — ``temperature``, ``top_p``, ``top_k``, ``repeat_penalty``, ``seed``,
``num_ctx``, ``num_predict``, … — that the OpenAI-compatible chat request carries
verbatim. We persist them per model ref (with a ``*`` global default) and thread
them into every chat call, mirroring the ``model.json`` / ``effort.json`` pattern.

Values are parsed as JSON so the user gets real types::

    num_ctx=8192      -> int 8192
    temperature=0.7   -> float 0.7
    verbose=true      -> bool True
    stop=["</s>"]     -> list
    model_tag=foo     -> str "foo"   (JSON parse fails -> kept as string)

Setting ``key=`` (empty value) or a bare ``key`` removes that key.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..util.jsonio import atomic_write, dump_json, load_json

# Structural request fields a tune value must never clobber — they define the
# request shape, not the sampling. (`max_tokens` is intentionally NOT reserved:
# a user may legitimately raise/lower a model's output ceiling per call.)
RESERVED = {"model", "messages", "tools", "tool_choice", "stream",
            "stream_options"}

GLOBAL = "*"  # the bucket that applies to every model ref


def _path(dot) -> Path:
    return Path(dot) / "model_opts.json"


def load_state(dot) -> dict:
    """Full persisted state: ``{ref | "*": {key: value}}``. A missing or
    truncated file degrades to empty, never crashes the session."""
    p = _path(dot)
    if not p.exists():
        return {}
    try:
        data = load_json(p)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(dot, state: dict) -> None:
    # drop empty buckets so a reset leaves a clean (or absent) file
    clean = {k: v for k, v in state.items() if isinstance(v, dict) and v}
    atomic_write(_path(dot), dump_json(clean))


def coerce(value: str):
    """Parse a CLI value with JSON semantics, falling back to the raw string."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def parse_kv(pairs) -> tuple[dict, list[str]]:
    """Split ``key=value`` tokens into ({set}, [unset]). ``key=`` (empty value)
    or a bare ``key`` unsets, so ``/model tune temperature`` clears it."""
    setk: dict = {}
    unset: list[str] = []
    for tok in pairs:
        if "=" not in tok:
            k = tok.strip()
            if k:
                unset.append(k)
            continue
        k, _, v = tok.partition("=")
        k = k.strip()
        if not k:
            continue
        if v == "":
            unset.append(k)
        else:
            setk[k] = coerce(v)
    return setk, unset


def set_opts(dot, ref: str, setk: dict, unset: list[str],
             glob: bool = False) -> dict:
    """Apply set/unset to the ref bucket (``*`` when ``glob``). Returns it."""
    state = load_state(dot)
    key = GLOBAL if glob else ref
    bucket = dict(state.get(key, {}) or {})
    for k in unset:
        bucket.pop(k, None)
    for k, v in setk.items():
        bucket[k] = v
    state[key] = bucket
    _save_state(dot, state)
    return bucket


def reset(dot, ref: str, glob: bool = False) -> None:
    state = load_state(dot)
    state.pop(GLOBAL if glob else ref, None)
    _save_state(dot, state)


def resolve_opts(dot, ref: str, config: Optional[dict] = None) -> dict:
    """Effective options for ``ref``: config ``[model].options`` ← ``*`` ←
    per-ref (later wins). RESERVED keys are stripped defensively so a stray
    tune can never break the wire request."""
    out: dict = {}
    cfg = ((config or {}).get("model", {}) or {}).get("options", {}) or {}
    if isinstance(cfg, dict):
        out.update(cfg)
    state = load_state(dot)
    out.update(state.get(GLOBAL, {}) or {})
    if ref:
        out.update(state.get(ref, {}) or {})
    return {k: v for k, v in out.items() if k not in RESERVED}
