"""The transcript as a working set over an external store (SPEC §8.9).

The loop appended to its transcript in ten places and never removed anything.
The only bound on a session was ``max_steps``, so a long triage did not finish —
it suffocated. Tool results dominate: one ``sim.run`` carries its summary,
diagnostics, artifact paths and metrics, and forty of those is a context window.

A general-purpose coding agent has to *summarize* to shed that weight, and
summarizing is lossy in a way nobody can audit. Chipchamp does not: its ground
truth is external, persistent and **addressable**. A job is ``J-0342``; its logs
are grep-able through ``job.log``, its verdict through ``job.status``, its waves
through ``wave.*``. The design DB, coverage sets and evidence bundles are the
same story.

So compaction here is not summarization. It is **eviction from a cache with a
guaranteed backing store**: an old tool result collapses to its verdict plus the
id needed to fetch the rest. What survives is chosen by what cannot be re-fetched
— the human's own words, the model's reasoning, the plan — versus what can.

Three rules the implementation is built around:

1. **Never evict what has no backing store.** User turns and assistant text are
   the only things in the transcript that exist nowhere else. They are kept
   whole, always.
2. **Never evict the recent window.** The model is mid-thought about the last
   few steps; compacting those would make it re-fetch what it just asked for.
3. **Always leave the address.** An evicted result keeps its job id and the name
   of the tool that made it, so the path back is in the transcript, not in a
   convention the model has to remember.
"""
from __future__ import annotations

import json
from typing import Optional

# Fields worth keeping verbatim in a digest: verdicts, addresses and counts.
# Everything else in a tool result is re-fetchable, and most of it is bulk.
KEEP_KEYS = ("job", "status", "sim_status", "verdict", "errors", "warnings",
             "accepted", "blocking_gates", "task_class", "coverage", "path",
             "seed", "cached", "denied", "error", "summary", "test", "top",
             "total_area_um2", "wns_ns", "slack_ns", "flake_rate")

# how many characters of a kept string field survive
FIELD_CLIP = 220

# a list/dict under a kept key survives only if it is small enough to BE a
# verdict rather than a body (a diagnostics array never is)
CONTAINER_KEEP = 200


def digest_output(name: str, output: str) -> tuple[str, bool]:
    """Shrink one tool result to its verdict + address. Returns (text, changed).

    A result that is already small is returned untouched — compaction that
    rewrote everything would churn the prompt cache for nothing.
    """
    if len(output) <= 400:
        return output, False
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        # not JSON (a raw log slice, a rendered table): keep the head, which is
        # where tools put their verdict, and say what was dropped
        head = output[:FIELD_CLIP]
        return (f"{head}… [{len(output) - len(head)} chars evicted from context; "
                f"re-read with job.log / fs.read]"), True
    if not isinstance(data, dict):
        return (f"[{len(output)}-char {name} result evicted from context; "
                f"re-run the tool to see it again]"), True
    kept = {}
    for k in KEEP_KEYS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, str):
            kept[k] = v[:FIELD_CLIP] + "…" if len(v) > FIELD_CLIP else v
        elif isinstance(v, (int, float, bool)) or v is None:
            kept[k] = v
        elif len(json.dumps(v, default=str)) <= CONTAINER_KEEP:
            kept[k] = v
        # else: a container too big to be a verdict. `errors` is the trap —
        # lint reports a COUNT and sim reports a list of diagnostics under the
        # same name, and keeping the list verbatim evicted nothing at all.
    dropped = sorted(set(data) - set(kept))
    pointer = _pointer(name, data)
    text = json.dumps(kept, default=str)
    if dropped:
        text += f" [evicted: {', '.join(dropped[:8])}"
        text += f" (+{len(dropped) - 8} more)" if len(dropped) > 8 else ""
        text += f" — {pointer}]"
    return text, True


def _pointer(name: str, data: dict) -> str:
    """How to get the evicted detail back. Naming the actual tool beats a
    generic 'it was truncated', which leaves the model guessing."""
    job = data.get("job") or data.get("waves_job")
    if job:
        return f"job {job}: job.log / job.status"
    if data.get("path"):
        return f"fs.read {data['path']}"
    return f"re-run {name}"


def compact(transcript: list[dict], *, budget_chars: int,
            keep_recent: int = 6) -> tuple[list[dict], dict]:
    """Evict old tool-result bodies until the transcript fits `budget_chars`.

    Oldest-first, because the recent window is what the model is reasoning
    about. Returns the new transcript and a report of what moved. Never touches
    user or assistant turns — those have no backing store.
    """
    from .loop import transcript_size
    before = transcript_size(transcript)["chars"]
    if before <= budget_chars:
        return transcript, {"compacted": False, "before": before, "after": before}

    out = [dict(m) for m in transcript]
    evicted = 0
    horizon = max(0, len(out) - keep_recent)

    def _sweep(skip_pinned: bool) -> bool:
        nonlocal evicted
        for i in range(horizon):
            msg = out[i]
            if msg.get("role") != "tool" \
                    or not isinstance(msg.get("content"), list):
                continue
            results = []
            for r in msg["content"]:
                if not isinstance(r, dict):
                    results.append(r)
                    continue
                # Library cards are REFERENCE material, not conversation: a
                # model that loses them re-derives interfaces from memory and
                # debugs against fiction (a lint queue rose 3->19 that way).
                # Pin them: evicted only in a second pass, when nothing else
                # can free enough space.
                if skip_pinned and str(r.get("name", "")).startswith("lib."):
                    results.append(r)
                    continue
                text, changed = digest_output(r.get("name", "?"),
                                              str(r.get("output", "")))
                if changed:
                    evicted += 1
                results.append({**r, "output": text})
            out[i] = {**msg, "content": results}
            if transcript_size(out)["chars"] <= budget_chars:
                return True
        return False

    if not _sweep(skip_pinned=True):
        _sweep(skip_pinned=False)
    after = transcript_size(out)["chars"]
    # The recent window is never sacrificed to hit a number: a few large recent
    # results can leave the transcript over budget, and that is the right
    # outcome — blinding the model to what it just did would cost more than the
    # overrun. `under_budget` says so plainly instead of implying success.
    return out, {"compacted": evicted > 0, "before": before, "after": after,
                 "evicted_results": evicted,
                 "under_budget": after <= budget_chars,
                 "freed": max(0, before - after)}


def budget_for(gateway, config: Optional[dict] = None) -> int:
    """Characters of transcript to allow before compacting.

    Prefers an explicit ``[model] context_chars``. Otherwise it derives from the
    model's own declared window (``num_ctx`` for the local servers) at ~3.6
    chars/token, spending 55% of it on the transcript — the rest is the system
    prompt, the tool schemas and room for the answer. With no signal at all it
    falls back to a value comfortable for a 32k model, since guessing large on
    a small local model is the failure this whole module exists to prevent."""
    cfg = (config or {}).get("model", {}) if config else {}
    explicit = cfg.get("context_chars")
    if explicit is not None:
        try:
            # 0 is a real answer, not a missing one: it means "never evict —
            # I would rather hit a hard wall than lose a body"
            return 0 if int(explicit) == 0 else max(8000, int(explicit))
        except (TypeError, ValueError):
            pass
    num_ctx = 0
    for src in (getattr(gateway, "options", None) or {}, cfg.get("options", {})):
        try:
            num_ctx = max(num_ctx, int(src.get("num_ctx") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    if num_ctx:
        return max(8000, int(num_ctx * 3.6 * 0.55))
    return 60_000
