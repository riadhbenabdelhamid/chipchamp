"""Workspace-local visual preferences, outside verification input manifests."""
from __future__ import annotations

import json

from .util.jsonio import atomic_write, dump_json

DEFAULTS = {"theme": "scope", "density": "compact", "motion": True}


def load(ws):
    values = {**DEFAULTS, **(ws.config.get("ui", {}) or {})}
    try:
        local = json.loads((ws.dot / "ui.json").read_text())
        if isinstance(local, dict):
            values.update({k: v for k, v in local.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if values.get("density") not in ("compact", "verbose"):
        values["density"] = DEFAULTS["density"]
    if not isinstance(values.get("motion"), bool):
        values["motion"] = True
    if not isinstance(values.get("theme"), str):
        values["theme"] = DEFAULTS["theme"]
    return values


def save(ws, key, value):
    if key not in DEFAULTS:
        raise ValueError(f"Unknown visual preference: {key}")
    path = ws.dot / "ui.json"
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError):
        values = {}
    if not isinstance(values, dict):
        values = {}
    values[key] = value
    atomic_write(path, dump_json(values))
    return path
