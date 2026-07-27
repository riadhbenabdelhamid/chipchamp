"""Autonomy levels (SPEC §11.2) and per-path resolution.

Autonomy is a policy dial per path × task class. No level ever permits the
hard-forbidden actions (activating waivers/exclusions, lowering coverage
baselines, editing policy, force-push, touching ACL'd paths) — those are checked
independently of level.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

LEVELS = {
    "L0": "suggest — read + propose diffs/reports; human applies everything",
    "L1": "edit with gates — edit tree, run V0–V2, launch V3; human merges",
    "L2": "branch autonomy — own branch, full ladder, open MR; human merges",
    "L3": "maintenance autonomy — auto-merge allow-listed classes on non-release branches",
}
_ORDER = ["L0", "L1", "L2", "L3"]

# actions no autonomy level may perform (SPEC §11.2)
FORBIDDEN_ALWAYS = [
    "activate_waiver", "activate_exclusion", "lower_coverage_baseline",
    "edit_policy", "force_push", "write_denied_path",
]

# classes L3 may auto-merge (very conservative)
L3_ALLOWED_CLASSES = {"docs"}


@dataclass
class AutonomyDecision:
    level: str
    may_edit: bool
    may_launch_jobs: bool
    may_open_mr: bool
    may_automerge: bool
    reason: str


def rank(level: str) -> int:
    return _ORDER.index(level) if level in _ORDER else 0


def resolve_level(path: str, rules: dict, default: str = "L1") -> str:
    """rules: {glob: {"max": "L1", ...}}. Most-specific (longest) glob wins; the
    per-path 'max' caps the level."""
    best_glob, best = None, default
    for glob, spec in rules.items():
        if fnmatch.fnmatch(path, glob):
            if best_glob is None or len(glob) > len(best_glob):
                best_glob = glob
                best = spec.get("max", default) if isinstance(spec, dict) else spec
    return best


def decide(task_class: str, paths: list[str], rules: dict, default: str = "L1") -> AutonomyDecision:
    # the effective level is the *minimum* cap across all touched paths
    level = default
    if paths:
        caps = [resolve_level(p, rules, default) for p in paths]
        level = min(caps, key=rank)
    r = rank(level)
    automerge = (level == "L3" and task_class in L3_ALLOWED_CLASSES)
    return AutonomyDecision(
        level=level,
        may_edit=r >= 1,
        may_launch_jobs=r >= 1,
        may_open_mr=r >= 2,
        may_automerge=automerge,
        reason=f"effective level {level} for classes touching {len(paths)} path(s)")
