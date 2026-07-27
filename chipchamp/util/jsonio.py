"""JSON persistence helpers with atomic writes.

Job records, the design DB, coverage snapshots and evidence bundles are all
persisted as JSON. Atomic writes prevent a crashed process from leaving a
half-written record (relevant to NFR-05: crash-safe job tracking).
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"Cannot serialize {type(obj).__name__}")


def dump_json(obj: Any, *, indent: int = 2, sort_keys: bool = False) -> str:
    return json.dumps(obj, indent=indent, default=_default, sort_keys=sort_keys)


def atomic_write(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
