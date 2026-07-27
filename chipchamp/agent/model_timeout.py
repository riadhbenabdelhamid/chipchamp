"""Per-model call budget (`/timeout`) — SPEC §14.

A slow local model needs a bigger per-call wall-clock budget than a fast one, so
the budget is stored PER MODEL REF (with a ``*`` global default), mirroring the
``effort.json`` / ``model_opts.json`` pattern. Resolution order (first hit wins):

    per-ref  →  global ``*``  →  config [model].request_timeout  →  None

``None`` means "use the provider default" (env ``CHIPCHAMP_MODEL_TIMEOUT`` or
300s). A stored ``0`` means **off** — no wall-clock limit (rely on streaming
content-liveness to catch a truly wedged endpoint).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..util.jsonio import atomic_write, dump_json, load_json

GLOBAL = "*"


def _path(dot) -> Path:
    return Path(dot) / "timeout.json"


def load_state(dot) -> dict:
    p = _path(dot)
    if not p.exists():
        return {}
    try:
        data = load_json(p)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(dot, state: dict) -> None:
    clean = {k: v for k, v in state.items() if isinstance(v, (int, float))}
    atomic_write(_path(dot), dump_json(clean))


def set_timeout(dot, ref: str, seconds: float, glob: bool = False) -> None:
    """Store a budget (seconds; 0 = off) for a ref, or the ``*`` global."""
    state = load_state(dot)
    state[GLOBAL if glob else ref] = float(max(0.0, seconds))
    _save_state(dot, state)


def reset(dot, ref: str, glob: bool = False) -> None:
    state = load_state(dot)
    state.pop(GLOBAL if glob else ref, None)
    _save_state(dot, state)


def resolve_timeout(dot, ref: str, config: Optional[dict] = None) -> Optional[float]:
    """Effective budget for ``ref``: per-ref → global → config → None (provider
    default). A stored ``0`` is 'off' and returns 0.0 (no wall-clock limit)."""
    state = load_state(dot)
    for key in ([ref] if ref else []) + [GLOBAL]:
        if key in state:
            return float(state[key])
    cfg = ((config or {}).get("model", {}) or {}).get("request_timeout")
    return float(cfg) if cfg is not None else None
