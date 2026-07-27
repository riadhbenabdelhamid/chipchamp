"""Model router (SPEC §14): pick the right model for a task and auto-switch when
the current one is failing.

The router is a thin policy layer over the existing model registry + gateway. It
ranks a configurable candidate *pool* by the capability a task/phase needs
(using :mod:`model_profiles`), builds gateways for the winners, and hands the
loop the next fallback when a failure signature fires (refusal / timeout / stall
/ give-up). It is OFF by default and a no-op unless enabled — single-model runs
are unchanged. All of it is configured at runtime via the ``/router`` command
and persisted to ``.chipchamp/router.json``.

The router only chooses *which* model runs; it never relaxes a gate. report.done
still validates every change against real job records regardless of routing.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .gateway import ModelGateway
from .model_profiles import CAPABILITIES, load_profiles
from .providers.registry import ModelRegistry, _make

TRIGGERS = ["refusal", "timeout", "stall", "giveup", "error"]
LAYERS = ["reactive", "proactive", "telemetry"]

# Bare-refusal phrasings (matched only on a short text-only turn — see is_refusal).
_REFUSAL = re.compile(
    r"\b(?:I(?:'m| am)?\s+(?:sorry[,]?\s+but\s+)?(?:I\s+)?"
    r"(?:can'?t|cannot|can not|won'?t|am unable to|is unable to)\s+"
    r"(?:continue|assist|help|proceed|complete|do that|comply)"
    r"|I\s+(?:can'?t|cannot)\s+continue"
    r"|unable to (?:continue|comply|assist|help))\b",
    re.IGNORECASE)


@dataclass
class RouterConfig:
    enabled: bool = False
    layers: dict = field(default_factory=lambda: {
        "reactive": True, "proactive": False, "telemetry": False})
    pool: list = field(default_factory=list)          # ["provider:model", ...]
    triggers: dict = field(default_factory=lambda: {t: True for t in TRIGGERS})
    chains: dict = field(default_factory=dict)         # capability -> [ref, ...]
    profile_overrides: dict = field(default_factory=dict)

    @classmethod
    def load(cls, dot: str | Path) -> "RouterConfig":
        p = Path(dot) / "router.json"
        cfg = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except Exception:
                return cfg
            cfg.enabled = bool(data.get("enabled", cfg.enabled))
            cfg.layers = {**cfg.layers, **(data.get("layers") or {})}
            cfg.pool = list(data.get("pool") or [])
            cfg.triggers = {**cfg.triggers, **(data.get("triggers") or {})}
            cfg.chains = dict(data.get("chains") or {})
            cfg.profile_overrides = dict(data.get("profile_overrides") or {})
        return cfg

    def save(self, dot: str | Path) -> str:
        p = Path(dot) / "router.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))
        return str(p)


# task-class (diff-based) → the capability its primary work needs
_TASK_NEED = {
    "docs": "planner", "tb_only": "verifier", "build_scripts": "planner",
    "regmap": "rtl_author", "rtl_nfc": "rtl_author", "rtl_functional": "rtl_author",
    "timing": "debugger", "cdc": "debugger", "physical": "verifier",
    "efpga-fabulous": "rtl_author", "fpga": "rtl_author",
}
# coarse run phase → capability
_PHASE_NEED = {"plan": "planner", "author": "rtl_author",
               "debug": "debugger", "verify": "verifier"}


def need_for_task(task_class: str) -> str:
    return _TASK_NEED.get(task_class, "rtl_author")


def need_for_phase(phase: str) -> str:
    return _PHASE_NEED.get(phase, "rtl_author")


class ModelRouter:
    def __init__(self, registry: ModelRegistry, cfg: RouterConfig,
                 config: dict | None = None):
        self.registry = registry
        self.cfg = cfg
        self.profiles = load_profiles(config, cfg.profile_overrides)

    # ---- selection ----------------------------------------------------------

    def _profile(self, ref: str):
        from .model_profiles import default_profile
        return self.profiles.get(ref) or default_profile(ref)

    def rank(self, need: str) -> list:
        """Pool refs ranked best-first for `need`. An explicit chain override
        for this capability wins (restricted to the pool, in its stated order);
        otherwise sort by effective score, breaking ties by `speed`."""
        pool = list(self.cfg.pool)
        chain = self.cfg.chains.get(need)
        if chain:
            ranked = [r for r in chain if r in pool]
            ranked += [r for r in pool if r not in ranked]  # append the rest
            return ranked
        return sorted(pool, key=lambda r: (self._profile(r).effective(need),
                                           self._profile(r).score("speed")),
                      reverse=True)

    def pick(self, need: str = "rtl_author") -> Optional[str]:
        r = self.rank(need)
        return r[0] if r else None

    def chain(self, need: str = "rtl_author") -> list:
        return self.rank(need)

    def next_fallback(self, current: str, need: str, tried: set) -> Optional[str]:
        for ref in self.rank(need):
            if ref != current and ref not in tried:
                return ref
        return None

    # ---- gateway construction (reuse registry + ModelGateway) ---------------

    def build_gateway(self, ref: str,
                      like: Optional[ModelGateway] = None) -> Optional[ModelGateway]:
        provider, _, model = ref.partition(":")
        cfg = self.registry.provider_configs().get(provider)
        if cfg is None:
            return None
        audit_path = getattr(getattr(like, "audit", None), "path", None)
        # Effort is re-resolved for the TARGET model from the persisted state
        # (the same decision point cli._build_gateway uses). Deriving it from
        # the in-flight gateway alone made one hop through a non-supporting
        # model erase the user's choice for every later supporting one. The
        # in-memory value is only a (gated) fallback for efforts that were
        # never persisted.
        from .effort import resolve_effort, supported_levels
        effort = resolve_effort(self.registry.dot, ref, self.registry.config)
        if not effort:
            carry = (getattr(like, "reasoning_effort", "") or "").strip().lower()
            levels = supported_levels(ref, self.registry.config)
            effort = carry if levels and carry in levels else ""
        # `/model tune` options + `/timeout` budget follow the model, re-resolved
        # for the TARGET ref (a hot-swap must carry the new model's settings, not
        # the old one's — a fast fallback shouldn't inherit a slow model's budget).
        from .model_opts import resolve_opts
        from .model_timeout import resolve_timeout
        options = resolve_opts(self.registry.dot, ref, self.registry.config)
        timeout = resolve_timeout(self.registry.dot, ref, self.registry.config)
        if timeout is None:
            timeout = getattr(like, "timeout", None)
        return ModelGateway(
            _make(cfg), model or cfg.default_model,
            audit_log=str(audit_path) if audit_path else None,
            max_tokens=getattr(like, "max_tokens", 16384),
            timeout=timeout, reasoning_effort=effort, options=options)

    # ---- failure signatures -------------------------------------------------

    def is_refusal(self, text: str) -> bool:
        """A short, bare refusal message (not a substantive answer that merely
        contains the word 'can't')."""
        t = (text or "").strip()
        return bool(t) and len(t) < 300 and _REFUSAL.search(t) is not None

    def trigger_on(self, name: str) -> bool:
        return self.cfg.enabled and self.cfg.layers.get("reactive", True) \
            and self.cfg.triggers.get(name, True)

    # ---- telemetry ----------------------------------------------------------

    def record_outcome(self, dot: str | Path, rec: dict) -> None:
        if not self.cfg.layers.get("telemetry"):
            return
        p = Path(dot) / "model_telemetry.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), **rec}) + "\n")


def telemetry_stats(dot: str | Path) -> dict:
    """Aggregate model_telemetry.jsonl into per-(model, task_class) outcome
    counts — the substrate for learned scores and `/router stats`."""
    p = Path(dot) / "model_telemetry.jsonl"
    agg: dict = {}
    if not p.exists():
        return agg
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        for model in (r.get("models_used") or [r.get("model", "?")]):
            key = f"{model}|{r.get('task_class', '?')}"
            a = agg.setdefault(key, {"model": model,
                                     "task_class": r.get("task_class", "?"),
                                     "runs": 0, "completed": 0,
                                     "refused": 0, "timed_out": 0})
            a["runs"] += 1
            a["completed"] += int(bool(r.get("completed")))
            a["refused"] += int(bool(r.get("refused")))
            a["timed_out"] += int(bool(r.get("timed_out")))
    return agg
