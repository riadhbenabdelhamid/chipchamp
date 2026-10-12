"""Read-only, target-scoped job freshness for the interactive status bar."""
from __future__ import annotations

import time

KINDS = ("lint", "sim", "synth")
EXTRA_KINDS = ("formal", "lec", "efpga-fabulous")


def invalidate(ctx):
    ctx._ui_job_cache = None


def job_states(ctx):
    target = getattr(ctx, "target_name", "")
    key = (str(getattr(getattr(ctx, "ws", None), "root", "")), target)
    now = time.monotonic()
    cached = getattr(ctx, "_ui_job_cache", None)
    if cached and cached[0] == key and now - cached[1] < 3:
        return cached[2]
    try:
        records = ctx.runner.list_jobs()
        latest = {}
        for rec in records:
            if getattr(rec, "target_name", "") != target:
                continue
            if rec.kind in KINDS + EXTRA_KINDS:
                latest[rec.kind] = rec
        fingerprint = ctx.verification_hash() if latest else ""
        states = {}
        for kind in KINDS + EXTRA_KINDS:
            rec = latest.get(kind)
            if rec is None:
                if kind in KINDS:
                    states[kind] = "NOT RUN"
                continue
            if rec.status in ("queued", "running"):
                states[kind] = rec.status.upper()
                continue
            states[kind] = _record_state(rec, fingerprint)
    except Exception:
        states = {kind: "UNKNOWN" for kind in KINDS}
    ctx._ui_job_cache = (key, now, states)
    return states


def _record_state(rec, fingerprint):
    from .jobs.inputs import snapshot_current
    from .util.hashing import hash_manifest
    known = bool(getattr(rec, "verification_hash", ""))
    if known and rec.verification_hash != fingerprint:
        return "STALE"
    if getattr(rec, "input_snapshot", None):
        known = True
        if not snapshot_current(rec.input_snapshot):
            return "STALE"
    elif getattr(rec, "inputs_hash", "") and getattr(rec, "input_files", None):
        known = True
        if rec.inputs_hash != hash_manifest(rec.input_files):
            return "STALE"
    if not known:
        return "UNKNOWN"
    return {"passed": "PASS", "failed": "FAIL", "error": "ERROR",
            "timeout": "TIMEOUT"}.get(rec.status, "UNKNOWN")
