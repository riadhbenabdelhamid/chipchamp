"""Semantic skill triggering (SPEC §15.3): select the few skills relevant to
THIS task by embedding similarity, instead of pasting the whole corpus index
into every prompt.

Why: the static index costs O(corpus) tokens on every model call (a 97-skill
corpus ≈ 2.5k tokens × 10–20 calls per task) and trigger fidelity decays as
the list grows — small local models don't reliably attend to line 63 of a
97-line list. Picking from a handful of pre-filtered candidates is both
cheaper and easier.

How: each skill's ``name + description`` (the "Use when…" trigger prose) is
embedded ONCE via any OpenAI-compatible ``/v1/embeddings`` endpoint — the
same local servers the model layer already speaks to (LM Studio, Ollama,
vLLM…) — and cached in ``.chipchamp/skills.vec.json`` keyed by content hash.
At task time the task text is embedded (one small local call per task, not
per step) and cosine-ranked against the corpus; the top-k ride in the system
prompt. A literal skill-name mention in the task always outranks cosine
(keyword beats vector), so explicit invocations stay deterministic.

Everything degrades: no endpoint, no embedding model, or any network error
falls back to the static compact index — semantic mode can never take down
the loop. Configure in ``[skills]``: ``index = "semantic"`` (the default),
``embed_provider`` / ``embed_model`` (auto-discovered when blank),
``top_k`` (default 5), ``min_score`` (default 0.25).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import embeddings as emb
from .embeddings import _RESOLVED  # noqa: F401  (re-exported: tests clear it)
from .util.jsonio import atomic_write, dump_json, load_json

_QUERY_TIMEOUT = 6.0    # s; one task embedding — a slow server must not stall the loop
_BUILD_TIMEOUT = 60.0   # s; first-time corpus embedding (one batched call)


def cache_path(ws) -> Path:
    return Path(ws.dot) / "skills.vec.json"


def _cfg(ws) -> dict:
    cfg = ws.skills_cfg if hasattr(ws, "skills_cfg") else {}
    return {"provider": str(cfg.get("embed_provider", "")).strip(),
            "model": str(cfg.get("embed_model", "")).strip(),
            "top_k": int(cfg.get("top_k", 5)),
            "min_score": float(cfg.get("min_score", 0.25))}


# ---- endpoint resolution (thin delegates to the shared client; kept as
# module-level names so callers/tests can monkeypatch them) ------------------


def resolve_endpoint(ws) -> tuple[str, str]:
    cfg = _cfg(ws)
    return emb.resolve_endpoint(ws, cfg["provider"], cfg["model"])


def _embed(ws, texts: list[str], timeout: float) -> list[list[float]]:
    cfg = _cfg(ws)
    return emb.embed(ws, texts, timeout, cfg["provider"], cfg["model"])


# ---- corpus vectors (cached) -------------------------------------------------


def _skill_key(s) -> str:
    return hashlib.sha256(f"{s.name}\n{s.description}".encode()).hexdigest()[:16]


def corpus_vectors(ws, skills: dict) -> dict[str, list[float]]:
    """name -> vector for every skill, embedding only new/changed ones."""
    _, model = resolve_endpoint(ws)
    p = cache_path(ws)
    cache = {}
    if p.exists():
        try:
            cache = load_json(p)
        except Exception:
            cache = {}
    if cache.get("model") != model:
        cache = {"model": model, "entries": {}}
    entries: dict = cache.setdefault("entries", {})

    todo = [(n, s) for n, s in skills.items()
            if entries.get(n, {}).get("hash") != _skill_key(s)]
    if todo:
        texts = [f"{s.name}: {s.description}" for _, s in todo]
        vecs = _embed(ws, texts, _BUILD_TIMEOUT)
        for (n, s), v in zip(todo, vecs):
            entries[n] = {"hash": _skill_key(s),
                          "vec": [round(x, 5) for x in v]}
        # drop skills that no longer exist
        for gone in set(entries) - set(skills):
            entries.pop(gone, None)
        atomic_write(p, dump_json(cache))
    return {n: e["vec"] for n, e in entries.items() if n in skills}


# ---- selection -----------------------------------------------------------------


_cosine = emb.cosine


def _mentioned(name: str, task_low: str) -> bool:
    """True only if the skill name appears as a WHOLE token in the task — both
    its literal (hyphenated) form and a spaces-for-hyphens form — flanked by
    non-word, non-hyphen chars. A bare substring match (`'run' in 'running'`,
    or a short name inside an unrelated word) must NOT count as an explicit
    mention, or it would win the +1.0 bonus and hijack selection."""
    for form in {name, name.replace("-", " ")}:
        if re.search(r"(?<![\w-])" + re.escape(form) + r"(?![\w-])", task_low):
            return True
    return False


def select(ws, task: str, skills: dict) -> list[tuple[str, float]]:
    """Ranked (name, score) for the task: cosine + a name-mention bonus that
    always outranks similarity (explicit invocation stays deterministic)."""
    cfg = _cfg(ws)
    vecs = corpus_vectors(ws, skills)
    tvec = _embed(ws, [task], _QUERY_TIMEOUT)[0]
    low = task.lower()
    scored: list[tuple[str, float]] = []
    for name, v in vecs.items():
        score = _cosine(tvec, v)
        if _mentioned(name.lower(), low):
            score += 1.0  # cosine is bounded by 1: a real mention always wins
        scored.append((name, score))
    scored.sort(key=lambda t: (-t[1], t[0]))
    picked = [t for t in scored if t[1] >= cfg["min_score"]][:cfg["top_k"]]
    # never select nothing from a non-empty corpus: keep the best 3 as seeds
    return picked or scored[:3]


def semantic_index(ws, task: str) -> str:
    """The task-scoped index block, or '' to signal 'use the static index'
    (no task text — e.g. a REPL banner — has nothing to rank against)."""
    if not (task or "").strip():
        return ""
    from .skills import discover, first_sentence
    skills = discover(ws)
    if not skills:
        return ""
    picked = select(ws, task, skills)
    lines = [f"- {n}: {first_sentence(skills[n].description)}"
             for n, _ in picked if n in skills]
    lines.append(f"({len(picked)} of {len(skills)} skills, selected by "
                 f"relevance to this task — skill.list shows the full catalog)")
    return "\n".join(lines)
