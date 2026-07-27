"""Reasoning effort — the ``/effort`` lever (SPEC §14).

A reasoning model exposes a knob that trades think-depth for turnaround. The
wire parameter is the OpenAI-compatible ``reasoning_effort`` (honoured by
gpt-oss on LM Studio/vLLM, and by OpenAI's o-series / gpt-5).

Only models known to accept the parameter are *offered* the control, and an
unset effort sends **no field at all** — a model that doesn't understand it
either ignores it or rejects the request, so the default must stay off.

:func:`resolve_effort` is the single decision point every gateway construction
site (CLI build, ``/model`` rebuild, router hot-swap) routes through, so the
file > env > config precedence and the per-model gate cannot drift between
paths. A level the user explicitly forced onto a model via ``/effort`` is
honoured for that exact ref only; a persisted ``"off"`` pins effort off and
masks env/config defaults.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from ..util.jsonio import atomic_write, dump_json, load_json

# Canonical ladder, shallow -> deep. A model may expose a subset or extend it
# (gpt-5 adds "minimal"), so callers must use supported_levels(), not this.
LEVELS = ["low", "medium", "high"]

# Built-in family table: (pattern over the lowercased "provider:model" ref) ->
# the ladder that family accepts. Patterns are boundary-anchored so a name that
# merely CONTAINS a family string (e.g. "somegpt-5.8b") is not claimed, while
# path/registry prefixes ("openai/gpt-oss-120b", "prod-o3-mini") still match.
# gpt-oss is measured: low/medium/high scale reasoning ~1.4k/2.5k/9.1k chars on
# LM Studio. The o-series/gpt-5 entries follow the documented API parameter.
_SUPPORT: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"(?:^|[:/-])gpt-oss"), ["low", "medium", "high"]),
    (re.compile(r"(?:^|[:/])gpt-5(?:$|[-.:])"), ["minimal", "low", "medium", "high"]),
    (re.compile(r"(?:^|[:/-])o[1345](?:$|[-:/])"), ["low", "medium", "high"]),
]


def _table(config: Optional[dict]) -> list[tuple[re.Pattern, list[str]]]:
    """The effective family table: config ``[model.effort_support]`` entries
    (pattern -> ladder) first so users can declare support for models the
    built-ins don't know, then the built-ins. A bad user pattern is skipped —
    it must not break model calls."""
    rows: list[tuple[re.Pattern, list[str]]] = []
    cfg = ((config or {}).get("model", {}) or {}).get("effort_support", {}) or {}
    for pat, lv in cfg.items():
        levels = lv if isinstance(lv, list) else [lv]
        clean = [str(x).strip().lower() for x in levels if str(x).strip()]
        if not clean:
            continue
        try:
            rows.append((re.compile(str(pat).lower()), clean))
        except re.error:
            continue
    return rows + _SUPPORT


def supported_levels(ref: str, config: Optional[dict] = None) -> Optional[list[str]]:
    """The effort ladder ``ref`` accepts, or None if it has no such control.

    ``ref`` is a full "provider:model" string (e.g. lmstudio:openai/gpt-oss-120b).
    ``config`` (the workspace config dict) may extend the built-in families:

        [model.effort_support]
        "deepseek-r1" = ["low", "medium", "high"]
    """
    r = (ref or "").lower()
    for pat, levels in _table(config):
        if pat.search(r):
            return list(levels)
    return None


# ---- persisted state ---------------------------------------------------------


def _path(dot) -> Path:
    return Path(dot) / "effort.json"


def _load_state(dot) -> dict:
    """The persisted {level, ref} state ({} when unset). Corruption-tolerant:
    this runs inside every gateway build (a startup path), so a hand-edited or
    truncated effort.json must degrade to 'unset', never crash the session."""
    p = _path(dot)
    if not p.exists():
        return {}
    try:
        data = load_json(p)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_effort(dot) -> str:
    """The persisted /effort choice ("" when unset; "off" pins effort off)."""
    return str(_load_state(dot).get("level", "") or "")


def save_effort(dot, level: str, ref: str = "") -> str:
    """Persist the effort choice — with the ref it was chosen on, so a level
    forced onto an unlisted model survives restarts for that model only.
    A falsy level clears the file (back to 'unset')."""
    p = _path(dot)
    if level:
        state: dict = {"level": level}
        if ref:
            state["ref"] = ref
        atomic_write(p, dump_json(state))
    elif p.exists():
        p.unlink()
    return str(p)


# ---- the decision point --------------------------------------------------------


def _gate(level: str, levels: Optional[list[str]], forced: bool = False) -> str:
    """Normalize a candidate level and admit it only if the model's ladder
    contains it (or the user explicitly forced it on this model)."""
    lv = (level or "").strip().lower()
    if not lv or lv == "off":
        return ""
    if levels and lv in levels:
        return lv
    return lv if forced else ""


def resolve_effort(dot, ref: str, config: Optional[dict] = None) -> str:
    """The reasoning_effort to actually SEND for ``ref`` ("" for none).

    Precedence: explicit /effort choice > CHIPCHAMP_MODEL_REASONING_EFFORT >
    ``[model] reasoning_effort`` config — each case-normalized and gated by the
    model's ladder, so a model that doesn't accept the parameter never receives
    it (a strict server would 400 every call). Exceptions: a persisted "off"
    pins effort off, masking env/config; a level the user forced onto this
    exact ref via /effort is honoured (they were warned once)."""
    levels = supported_levels(ref, config)
    st = _load_state(dot)
    saved = str(st.get("level", "") or "").strip().lower()
    if saved == "off":
        return ""
    if saved:
        got = _gate(saved, levels,
                    forced=(str(st.get("ref", "") or "") == (ref or "")))
        if got:
            return got
    from ..brand import env as _brand_env
    got = _gate(_brand_env("MODEL_REASONING_EFFORT", ""), levels)
    if got:
        return got
    mcfg = (config or {}).get("model", {}) or {}
    return _gate(str(mcfg.get("reasoning_effort", "") or ""), levels)
