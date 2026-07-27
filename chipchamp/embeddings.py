"""Shared local-embedding client (SPEC §14): a thin, dependency-free wrapper
over any OpenAI-compatible ``/v1/embeddings`` endpoint — the same local servers
the model layer already speaks to (LM Studio, Ollama, vLLM…).

Two consumers build on this: semantic skill triggering (:mod:`skills_semantic`)
and semantic IP-component matching (:mod:`library_semantic`). Both embed a
corpus ONCE (cached on disk, keyed by content hash + model) and cosine-rank a
query against it. Everything degrades: no reachable endpoint / no embedding
model / any network error raises, and callers fall back to non-semantic paths.
"""
from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path
from typing import Callable

from .util.jsonio import atomic_write, dump_json, load_json

# (ws.root, provider, model) -> (base_url, resolved_model). Process-lifetime
# cache so repeated calls in one session don't re-probe the endpoint.
_RESOLVED: dict[tuple, tuple[str, str]] = {}


# ---- HTTP ----------------------------------------------------------------------


def _get(url: str, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post(url: str, payload: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _candidate_bases(ws, provider: str) -> list[str]:
    """base_urls to probe: the named provider, else every local preset +
    custom ``[providers.*]`` endpoints from config."""
    from .agent.providers.base import PRESETS
    bases: list[str] = []
    custom = (ws.config.get("providers", {}) or {}) if hasattr(ws, "config") else {}
    if provider:
        if provider in PRESETS:
            bases.append(PRESETS[provider].base_url)
        p = custom.get(provider, {})
        if p.get("base_url"):
            bases.append(p["base_url"])
    else:
        bases.extend(p.base_url for p in PRESETS.values() if p.local)
        bases.extend(p.get("base_url") for p in custom.values()
                     if p.get("base_url"))
    return [b.rstrip("/") for b in bases if b]


def resolve_endpoint(ws, provider: str = "", model: str = "") -> tuple[str, str]:
    """(base_url, embedding_model): the configured endpoint, else auto-discover
    the first reachable local server advertising a model id containing
    'embed'. Raises LookupError when none is reachable."""
    key = (str(ws.root), provider, model)
    if key in _RESOLVED:
        return _RESOLVED[key]
    for base in _candidate_bases(ws, provider):
        try:
            data = _get(f"{base}/models")
        except Exception:
            continue
        ids = [m.get("id", "") for m in data.get("data", [])]
        mdl = model or next((i for i in ids if "embed" in i.lower()), "")
        if model and model not in ids:
            continue  # a pinned model must exist on that endpoint
        if mdl:
            _RESOLVED[key] = (base, mdl)
            return base, mdl
    raise LookupError("no embedding endpoint (no reachable /v1 server with an "
                      "embedding model; set embed_provider/embed_model)")


def embed(ws, texts: list[str], timeout: float, provider: str = "",
          model: str = "") -> list[list[float]]:
    base, mdl = resolve_endpoint(ws, provider, model)
    data = _post(f"{base}/embeddings", {"model": mdl, "input": texts}, timeout)
    rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    vecs = [r.get("embedding") for r in rows]
    if len(vecs) != len(texts) or any(not v for v in vecs):
        raise ValueError(f"embedding endpoint returned {len(vecs)} vectors "
                         f"for {len(texts)} inputs")
    return vecs


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


# ---- cached corpus vectors -----------------------------------------------------


def cached_vectors(ws, items: dict, *, text_of: Callable, hash_of: Callable,
                   cache_file: Path, provider: str = "", model: str = "",
                   build_timeout: float = 60.0) -> dict[str, list[float]]:
    """name -> vector for every item, embedding only new/changed ones (keyed by
    ``hash_of(item)``) and persisting to ``cache_file``. The cache is discarded
    wholesale when the embedding model changes."""
    _, mdl = resolve_endpoint(ws, provider, model)
    cache: dict = {}
    if cache_file.exists():
        try:
            cache = load_json(cache_file)
        except Exception:
            cache = {}
    if cache.get("model") != mdl:
        cache = {"model": mdl, "entries": {}}
    entries: dict = cache.setdefault("entries", {})

    todo = [(n, o) for n, o in items.items()
            if entries.get(n, {}).get("hash") != hash_of(o)]
    if todo:
        vecs = embed(ws, [text_of(o) for _, o in todo], build_timeout,
                     provider, model)
        for (n, o), v in zip(todo, vecs):
            entries[n] = {"hash": hash_of(o), "vec": [round(x, 5) for x in v]}
        for gone in set(entries) - set(items):  # drop items that no longer exist
            entries.pop(gone, None)
        atomic_write(cache_file, dump_json(cache))
    return {n: e["vec"] for n, e in entries.items() if n in items}
