"""Semantic + interface IP-component matching (SPEC §12 reuse posture).

The problem: when the agent decomposes an architecture it names sub-blocks in
its OWN vocabulary ("input stream buffer", "elastic handshake stage",
"ping-pong accumulator") that rarely matches a library component's name/tags
lexically — so keyword `lib.search` misses the right part. This module matches
on **meaning + interface** instead of names, using two signals from the
manifest:

1. **Functional similarity** — embed each component's
   ``name + summary + description + tags + interface-signature`` once (cached,
   like skills), and cosine-rank the agent's behavioral description against it.
   Name-independent: "streaming buffer with backpressure" finds ``fifo_sync``.
2. **Interface compatibility** — a structured score from the component's
   ``interfaces`` / port ``role``s / ``clocks`` / ``resets``: a semantic match
   with an incompatible interface (wrong protocol, wrong clock-domain count) is
   down-ranked, and every candidate arrives with a machine-readable interface
   signature so the agent can judge "close enough" and map ports.

Degrades: with no embedding endpoint, `match` falls back to lexical + interface
scoring so it still returns useful candidates. Configure under ``[library]``:
``embed_provider`` / ``embed_model`` (auto-discovered when blank), ``top_k``,
``min_score``.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import embeddings as emb
from .library import LibModule, discover

_QUERY_TIMEOUT = 6.0
_BUILD_TIMEOUT = 90.0   # a 130-component corpus in one batched call


def cache_path(ws) -> Path:
    return Path(ws.dot) / "library.vec.json"


def _cfg(ws) -> dict:
    cfg = ws.library_cfg if hasattr(ws, "library_cfg") else {}
    return {"provider": str(cfg.get("embed_provider", "")).strip(),
            "model": str(cfg.get("embed_model", "")).strip(),
            "top_k": int(cfg.get("top_k", 6)),
            "min_score": float(cfg.get("min_score", 0.2))}


# ---- interface signature (structured metadata -> text) -------------------------


def _norm_proto(t: str) -> str:
    t = t.lower().strip()
    for a, b in (("axi4-lite", "axil"), ("axi-lite", "axil"), ("axilite", "axil"),
                 ("axi4-stream", "axis"), ("axi-stream", "axis"),
                 ("axi4", "axi"), ("wishbone", "wb"), ("tilelink", "tl"),
                 ("tilelink-ul", "tl"), ("valid_ready", "valid/ready"),
                 ("ready_valid", "valid/ready")):
        t = t.replace(a, b)
    return t


def _mod_protocols(mod: LibModule) -> set[str]:
    """Protocol vocabulary a component speaks — from its declared interfaces,
    category, tags, and a valid/ready port pair."""
    meta = mod.meta
    toks: set[str] = set()
    for i in meta.get("interfaces") or []:
        t = _norm_proto(str(i.get("type", "") or i.get("protocol", "")))
        if t:
            toks.add(t)
    for extra in (mod.category, " ".join(str(x) for x in meta.get("tags", []) or [])):
        for p in ("axil", "axis", "axi", "apb", "ahb", "wb", "tl", "wishbone",
                  "tilelink", "stream"):
            if p in _norm_proto(extra):
                toks.add("wb" if p == "wishbone" else "tl" if p == "tilelink" else p)
    roles = {str(p.get("role", "")).lower() for p in meta.get("ports", []) or []}
    if {"valid", "ready"} <= roles:
        toks.add("valid/ready")
    return toks


def interface_signature(mod: LibModule) -> str:
    """A compact, embedding- and human-friendly description of the interface."""
    meta = mod.meta
    protos = sorted({_norm_proto(str(i.get("type", "") or i.get("protocol", "")))
                     for i in meta.get("interfaces", []) or []
                     if (i.get("type") or i.get("protocol"))})
    roles: dict[str, int] = {}
    for p in meta.get("ports", []) or []:
        r = str(p.get("role", "")).lower()
        if r:
            roles[r] = roles.get(r, 0) + 1
    nclk = len(meta.get("clocks", []) or [])
    nrst = len(meta.get("resets", []) or [])
    params = ", ".join(str(p.get("name", "")) for p in meta.get("params", []) or []
                       if p.get("name"))
    parts = []
    if protos:
        parts.append("protocols: " + ", ".join(protos))
    if {"valid", "ready"} <= set(roles):
        parts.append("valid/ready handshake")
    if roles:
        parts.append("ports: " + " ".join(f"{r}x{n}" for r, n in sorted(roles.items())))
    parts.append(f"clocks: {nclk} ({'async/dual-clock' if nclk >= 2 else 'single-clock'})"
                 f"; resets: {nrst}")
    if params:
        parts.append("params: " + params)
    return "; ".join(parts)


def component_text(mod: LibModule) -> str:
    meta = mod.meta
    tags = ", ".join(str(t) for t in meta.get("tags", []) or [])
    return (f"{mod.name} [{mod.category}]. {mod.summary} "
            f"{meta.get('description', '')} tags: {tags}. "
            f"interface: {interface_signature(mod)}")


def _hash(mod: LibModule) -> str:
    return hashlib.sha256(component_text(mod).encode()).hexdigest()[:16]


def component_vectors(ws, mods: dict) -> dict[str, list[float]]:
    cfg = _cfg(ws)
    return emb.cached_vectors(ws, mods, text_of=component_text, hash_of=_hash,
                              cache_file=cache_path(ws), provider=cfg["provider"],
                              model=cfg["model"], build_timeout=_BUILD_TIMEOUT)


# ---- scoring -------------------------------------------------------------------


_QUERY_PROTOS = ("axil", "axi-lite", "axi4-lite", "axis", "axi-stream", "axi",
                 "apb", "ahb", "wishbone", "wb", "tilelink", "tl")


def _query_protocols(query_low: str) -> set[str]:
    toks = set()
    q = _norm_proto(query_low)
    for p in _QUERY_PROTOS:
        if re.search(r"(?<![a-z])" + re.escape(_norm_proto(p)) + r"(?![a-z])", q):
            toks.add(_norm_proto(p))
    return toks


def interface_compat(query_low: str, mod: LibModule) -> tuple[float, list[str]]:
    """Structured 0..1 compatibility of the query's interface hints with the
    component, plus human-readable reasons. Only dimensions the query actually
    constrains are scored (an unstated dimension is neutral, not penalized)."""
    reasons: list[str] = []
    dims = 0
    hits = 0.0

    qprot = _query_protocols(query_low)
    if qprot:
        dims += 1
        common = qprot & _mod_protocols(mod)
        if common:
            hits += 1
            reasons.append("protocol " + "/".join(sorted(common)))

    if re.search(r"valid|ready|handshake|back ?pressure|elastic", query_low):
        dims += 1
        roles = {str(p.get("role", "")).lower() for p in mod.meta.get("ports", []) or []}
        if {"valid", "ready"} <= roles:
            hits += 1
            reasons.append("valid/ready handshake")

    if re.search(r"async|cdc|dual[- ]?clock|two[- ]?clock|cross[- ]?clock|"
                 r"clock[- ]?domain|multi[- ]?clock", query_low):
        dims += 1
        if len(mod.meta.get("clocks", []) or []) >= 2:
            hits += 1
            reasons.append("2 clock domains (CDC)")
        elif "single" in query_low or "one clock" in query_low:
            hits += 1

    return (hits / dims if dims else 0.5), reasons


def _lexical(query_low: str, mod: LibModule) -> float:
    """0..1 lexical overlap fallback (used when embeddings are unavailable)."""
    words = set(re.findall(r"[a-z0-9]+", query_low)) - {"a", "the", "of", "with",
                                                         "and", "for", "that"}
    if not words:
        return 0.0
    tags = {str(t).lower() for t in mod.meta.get("tags", []) or []}
    name = mod.name.lower()
    hay = set(re.findall(r"[a-z0-9]+", (mod.summary + " " + mod.category + " "
              + str(mod.meta.get("description", ""))).lower()))
    score = 0.0
    for w in words:
        if w in tags or w in name:
            score += 2
        elif w in hay:
            score += 1
    return min(1.0, score / (2 * len(words)))


def match(ws, behavior: str, interface: str = "", category: str = "",
          limit: int = 6) -> list[tuple[LibModule, float, dict]]:
    """Ranked ``(component, score, info)`` for a described sub-block. `info`
    carries the per-signal breakdown, the interface signature, and reasons.
    Hybrid: semantic (embeddings) + interface compatibility + lexical; falls
    back to lexical + interface when no embedding endpoint is reachable."""
    mods = discover(ws)
    if category:
        mods = {n: m for n, m in mods.items()
                if m.category.lower().startswith(category.lower())}
    if not mods:
        return []
    query = (behavior + " " + interface).strip()
    qlow = query.lower()

    sem: dict[str, float] = {}
    have_sem = False
    try:
        vecs = component_vectors(ws, mods)
        cfg = _cfg(ws)
        qvec = emb.embed(ws, [query], _QUERY_TIMEOUT, cfg["provider"],
                         cfg["model"])[0]
        sem = {n: emb.cosine(qvec, v) for n, v in vecs.items()}
        have_sem = bool(sem)
    except Exception:
        have_sem = False

    out = []
    for n, m in mods.items():
        s = sem.get(n, 0.0)
        ic, why = interface_compat(qlow, m)
        lx = _lexical(qlow, m)
        if have_sem:
            score = 0.60 * s + 0.25 * ic + 0.15 * lx
        else:
            score = 0.60 * lx + 0.40 * ic
        out.append((m, score, {
            "semantic": round(s, 3), "interface_score": round(ic, 3),
            "lexical": round(lx, 3), "interface_signature": interface_signature(m),
            "why": why}))
    out.sort(key=lambda t: (-t[1], t[0].name))
    thr = _cfg(ws)["min_score"]
    kept = [t for t in out if t[1] >= thr][:limit]
    return kept or out[:min(3, limit)]  # never return nothing from a non-empty lib
