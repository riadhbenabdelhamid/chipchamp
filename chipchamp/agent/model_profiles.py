"""Per-model capability profiles for the model router (SPEC §14).

A profile scores a model 0..1 on a small set of agentic-RTL capabilities and
tags its known failure modes. The router ranks a candidate pool by the
capability a task/phase needs, and demotes models with disqualifying failure
modes. Built-in defaults are seeded from real observed behaviour; a project can
override any of it via ``[model.profiles]`` in config or through ``/router``.

Profiles are *data*, never authority — they only influence which model the
router tries first and in what fallback order. Every gate still validates
against real job records regardless of which model produced the work.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# The capabilities a task/phase can need. `speed` is a tie-breaker preference,
# not a quality axis.
CAPABILITIES = ["rtl_author", "debugger", "planner", "verifier", "speed"]

# Failure-mode tags and the ranking demerit each applies (subtracted from the
# capability score). `refuses` is effectively disqualifying; the rest are soft
# penalties (the loop's nudge/gates already contain most of their damage).
FAILURE_DEMERITS = {
    "refuses": 0.9,          # aborts benign tasks — avoid as a primary pick
    "narrates": 0.1,         # describes instead of acting (nudge now mitigates)
    "fabricates": 0.1,       # claims success in prose (gates + nudge catch it)
    "premature_ship": 0.1,   # tries report.done/mr before the work is done
    "no_reuse": 0.15,        # ignores the library, writes monolithic RTL
    "slow": 0.0,             # informational; already reflected in `speed`
}

NEUTRAL = 0.5  # default score for an unprofiled model on an unlisted capability


@dataclass
class ModelProfile:
    ref: str                                  # "provider:model"
    scores: dict = field(default_factory=dict)        # capability -> 0..1
    failure_modes: list = field(default_factory=list)
    note: str = ""

    def score(self, need: str) -> float:
        return float(self.scores.get(need, NEUTRAL))

    def demerit(self) -> float:
        return sum(FAILURE_DEMERITS.get(f, 0.0) for f in self.failure_modes)

    def effective(self, need: str) -> float:
        """Score for ranking: capability minus failure-mode demerits."""
        return self.score(need) - self.demerit()


def _p(ref, note="", modes=None, **scores) -> ModelProfile:
    return ModelProfile(ref=ref, scores=scores, failure_modes=list(modes or []),
                        note=note)


# Seeded from the multi-model campaign (2026-07-13). Refs are provider:model.
DEFAULT_PROFILES: dict = {p.ref: p for p in [
    _p("lmstudio:qwen/qwen3.6-35b-a3b",
       note="best debugger: iterates lint to clean, reaches passing sim; slow",
       modes=["slow"],
       rtl_author=0.7, debugger=0.9, planner=0.75, verifier=0.85, speed=0.2),
    _p("lmstudio:openai/gpt-oss-120b",
       note="best RTL author (clean reuse instantiation); weak self-diagnosis",
       modes=["narrates", "fabricates"],
       rtl_author=0.9, debugger=0.45, planner=0.9, verifier=0.6, speed=0.6),
    _p("lmstudio:openai/gpt-oss-20b",
       note="fast but chronically refuses agentic tasks",
       modes=["refuses"],
       rtl_author=0.5, debugger=0.3, planner=0.5, verifier=0.3, speed=0.9),
    _p("lmstudio:nvidia/nemotron-3-nano-4b",
       note="fast, reuses+decomposes, but RTL does not lint clean",
       rtl_author=0.45, debugger=0.35, planner=0.6, verifier=0.35, speed=0.8),
    _p("lmstudio:nvidia/nemotron-3-nano-omni",
       note="analysis paralysis — narrates architecture, rarely acts",
       modes=["narrates"],
       rtl_author=0.25, debugger=0.25, planner=0.45, verifier=0.25, speed=0.5),
    _p("lmstudio:nvidia.orchestrator-8b",
       note="no reuse, monolithic RTL, ships prematurely",
       modes=["no_reuse", "premature_ship"],
       rtl_author=0.3, debugger=0.3, planner=0.35, verifier=0.25, speed=0.7),
]}

# Generic high-all-round default for frontier hosted models, matched by prefix.
_FRONTIER_PREFIXES = ("anthropic:", "openai:gpt-4", "openai:o1", "openai:o3")
_FRONTIER = ModelProfile(
    ref="", note="frontier hosted model (generic strong all-round default)",
    scores={"rtl_author": 0.9, "debugger": 0.9, "planner": 0.9,
            "verifier": 0.9, "speed": 0.8})


def default_profile(ref: str) -> ModelProfile:
    """Best built-in profile for a ref: exact match, else frontier default by
    prefix, else a neutral profile (still usable, just unopinionated)."""
    if ref in DEFAULT_PROFILES:
        return DEFAULT_PROFILES[ref]
    if any(ref.startswith(pfx) for pfx in _FRONTIER_PREFIXES):
        return replace(_FRONTIER, ref=ref)
    return ModelProfile(ref=ref, note="unprofiled — neutral defaults")


def load_profiles(config: dict | None = None,
                  overrides: dict | None = None) -> dict:
    """Merge, in increasing precedence: built-in DEFAULT_PROFILES ← config
    ``[model.profiles]`` ← router.json ``profile_overrides``. Each source maps a
    ref to a partial ``{scores?, failure_modes?, note?}`` that patches the base.
    Returns ``{ref: ModelProfile}`` for every ref mentioned anywhere."""
    out: dict = {ref: replace(p) for ref, p in DEFAULT_PROFILES.items()}

    def apply(src: dict | None):
        for ref, spec in (src or {}).items():
            base = out.get(ref) or default_profile(ref)
            scores = {**base.scores, **(spec.get("scores") or {})}
            out[ref] = ModelProfile(
                ref=ref, scores=scores,
                failure_modes=spec.get("failure_modes", base.failure_modes),
                note=spec.get("note", base.note))

    apply((config or {}).get("model", {}).get("profiles"))
    apply(overrides)
    return out
