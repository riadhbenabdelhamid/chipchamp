"""Identifier generation.

Job ids are sequential (``J-0001``) so a session's job history is stable and
readable (SPEC Appendix B uses ``J-8842``-style ids). Session ids embed a
timestamp for sorting. ``deterministic_id`` derives a stable id from content,
used where reproducibility matters more than uniqueness.
"""
from __future__ import annotations

import os
import time

from .hashing import sha256_text


def new_job_id(seq: int) -> str:
    return f"J-{seq:04d}"


def new_session_id() -> str:
    # Monotonic-ish, human-sortable; not security-sensitive.
    return f"S-{int(time.time())}-{os.getpid() % 10000:04d}"


def deterministic_id(prefix: str, *parts: str, n: int = 10) -> str:
    return f"{prefix}-{sha256_text('|'.join(parts))[:n]}"
