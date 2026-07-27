"""Model registry & discovery (SPEC §14; supports the ``/model`` selector).

Merges built-in presets with any custom ``[providers.*]`` from config, resolves
the *current selection* (selection file > env > config > key-based auto), can
enumerate accessible models per provider for the picker, and persists the user's
choice to ``.chipchamp/model.json``.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ...util.jsonio import atomic_write, dump_json, load_json
from ...brand import env_name as _env_name
from .anthropic_provider import AnthropicProvider
from .base import PRESETS, ModelInfo, Provider, ProviderConfig
from .openai_compat import OpenAICompatProvider


def _make(cfg: ProviderConfig) -> Provider:
    if cfg.kind == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAICompatProvider(cfg)


class ModelRegistry:
    def __init__(self, config: Optional[dict] = None, dot_dir: str = ".chipchamp"):
        self.config = config or {}
        self.dot = Path(dot_dir)

    # ---- provider configs ---------------------------------------------------

    def provider_configs(self) -> dict[str, ProviderConfig]:
        cfgs: dict[str, ProviderConfig] = {k: replace(v) for k, v in PRESETS.items()}
        for name, spec in (self.config.get("providers") or {}).items():
            base = cfgs.get(name)
            cfgs[name] = ProviderConfig(
                name=name, kind=spec.get("kind", base.kind if base else "openai"),
                base_url=spec.get("base_url", base.base_url if base else ""),
                api_key=spec.get("api_key", base.api_key if base else ""),
                api_key_env=spec.get("api_key_env", base.api_key_env if base else ""),
                default_model=spec.get("default_model", base.default_model if base else ""),
                local=spec.get("local", base.local if base else False),
                extra_headers=spec.get("extra_headers", {}))
        return cfgs

    def provider(self, name: str) -> Optional[Provider]:
        cfg = self.provider_configs().get(name)
        return _make(cfg) if cfg else None

    # ---- discovery (for the /model picker) ----------------------------------

    def discover(self) -> list[tuple[Provider, list[ModelInfo]]]:
        """Return (provider, models) for every provider that is reachable. Probes
        local endpoints, so this may take a moment; used only by `chipchamp model`."""
        out = []
        for cfg in self.provider_configs().values():
            prov = _make(cfg)
            if prov.available():
                out.append((prov, prov.list_models()))
        return out

    # ---- selection ----------------------------------------------------------

    def _selection_path(self) -> Path:
        return self.dot / "model.json"

    def saved_selection(self) -> Optional[dict]:
        p = self._selection_path()
        return load_json(p) if p.exists() else None

    def save_selection(self, provider: str, model: str,
                       base_url: str = "", api_key_env: str = "") -> str:
        sel = {"provider": provider, "model": model}
        if base_url:
            sel["base_url"] = base_url
        if api_key_env:
            sel["api_key_env"] = api_key_env
        atomic_write(self._selection_path(), dump_json(sel))
        return str(self._selection_path())

    def current(self) -> Optional[dict]:
        """The active {provider, model, ...} from selection file > env > config."""
        sel = self.saved_selection()
        if sel:
            return sel
        from ...brand import env as benv
        if benv("PROVIDER"):
            return {"provider": benv("PROVIDER"),
                    "model": benv("MODEL", ""),
                    "base_url": benv("BASE_URL", "")}
        mcfg = self.config.get("model") or {}
        if mcfg.get("provider"):
            return {"provider": mcfg["provider"], "model": mcfg.get("model", ""),
                    "base_url": mcfg.get("base_url", "")}
        # key-based auto (no network probe)
        if os.environ.get("ANTHROPIC_API_KEY"):
            return {"provider": "anthropic", "model": ""}
        if os.environ.get("OPENAI_API_KEY"):
            return {"provider": "openai", "model": ""}
        return None

    def resolve(self, role: str | None = None) -> tuple[Optional[Provider], str, str]:
        """Return (provider, model, reason). provider is None if nothing usable.

        `role` consults the routing table (SPEC §14): ``[model.routing]`` maps
        roles (planner / subagent / summarizer / bulk) to ``provider:model``
        refs, so e.g. triage-analyst subagents can run on a cheaper local model
        than the planner. Unrouted roles fall back to the default selection."""
        if role:
            routing = (self.config.get("model") or {}).get("routing") or {}
            ref = routing.get(role)
            if ref and ":" in ref:
                prov_name, _, model = ref.partition(":")
                cfg = self.provider_configs().get(prov_name)
                if cfg:
                    return _make(cfg), model or cfg.default_model, "ok"
        sel = self.current()
        if not sel:
            return None, "", ("no model configured — run `chipchamp model` to pick one, "
                              f"or set {_env_name('PROVIDER')} / ANTHROPIC_API_KEY / OPENAI_API_KEY")
        cfgs = self.provider_configs()
        cfg = cfgs.get(sel["provider"])
        if cfg is None:
            # allow an ad-hoc openai-compatible endpoint by base_url
            cfg = ProviderConfig(name=sel["provider"], kind="openai",
                                 base_url=sel.get("base_url", ""),
                                 api_key=benv("API_KEY", "-"),
                                 local=True)
        if sel.get("base_url"):
            cfg = replace(cfg, base_url=sel["base_url"])
        if sel.get("api_key_env"):
            cfg = replace(cfg, api_key_env=sel["api_key_env"])
        model = sel.get("model") or cfg.default_model
        return _make(cfg), model, "ok"
