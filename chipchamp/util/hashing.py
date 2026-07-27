"""Content hashing for reproducibility and the content-addressed artifact store.

Reproducibility is a first-class requirement (SPEC §6 P7, FR-JOB-01). Every job
records the hashes of its inputs so ``chipchamp repro`` can prove that a replay
saw byte-identical sources.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(value: str, n: int = 12) -> str:
    return value[:n]


def hash_manifest(paths: Iterable[str | Path]) -> str:
    """Order-independent hash of a set of files (a filelist fingerprint)."""
    parts = []
    for p in sorted(str(x) for x in paths):
        try:
            parts.append(f"{p}:{sha256_file(p)}")
        except OSError:
            parts.append(f"{p}:MISSING")
    return sha256_text("\n".join(parts))
