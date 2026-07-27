"""Adapter registry — role-based selection with capability manifests (SPEC §8.4).

The planner asks the registry for an adapter by role (``sim``, ``lint``, ...),
honoring the project's tool selection (config ``[tools]``) and falling back to
the first *available* adapter for that role. Capability manifests let the planner
warn about simulator divergence or missing features before running.
"""
from __future__ import annotations

from .base import Adapter
from .cocotb_adapter import CocotbAdapter
from .eqy import EqyAdapter
from .fabulous import FabulousAdapter
from .ghdl import GhdlAdapter
from .icarus import IcarusAdapter
from .librelane import LibreLaneAdapter
from .nextpnr import NextpnrAdapter
from .opensta import OpenStaAdapter
from .questa import QuestaAdapter
from .symbiyosys import SymbiYosysAdapter
from .vcs import PrimeTimeAdapter, VcsAdapter
from .verible import VeribleAdapter
from .verilator import VerilatorAdapter
from .vivado import VivadoAdapter
from .yosys import YosysAdapter

_BUILTIN: dict[str, type[Adapter]] = {
    "verilator": VerilatorAdapter,
    "icarus": IcarusAdapter,
    "verible": VeribleAdapter,
    "yosys": YosysAdapter,
    "eqy": EqyAdapter,
    "symbiyosys": SymbiYosysAdapter,
    "ghdl": GhdlAdapter,
    "cocotb": CocotbAdapter,
    "opensta": OpenStaAdapter,
    "librelane": LibreLaneAdapter,
    "fabulous": FabulousAdapter,
    "nextpnr": NextpnrAdapter,
    "vivado": VivadoAdapter,
    # commercial tier (M2): glue for customer-licensed installs; normalizers
    # are fixture-validated in the test suite
    "questa": QuestaAdapter,
    "vcs": VcsAdapter,
    "primetime": PrimeTimeAdapter,
}

# default adapter to use for each role when config doesn't pin one
_ROLE_DEFAULTS = {
    "sim": ["icarus", "verilator"],
    "lint": ["verilator", "verible"],
    "format": ["verible"],
    "coverage": ["verilator"],
    "synth": ["yosys"],
    "elab": ["verilator", "yosys", "ghdl"],
    "lec": ["eqy"],
    "formal": ["symbiyosys"],
    "sta": ["opensta", "yosys"],  # yosys = structural-depth proxy fallback
    "pnr": ["librelane"],          # RTL-to-GDSII (physical vertical)
    "efpga-fabulous": ["fabulous"],         # embedded FPGA fabrics + bitstreams
    "fpga": ["nextpnr", "vivado"],  # FPGA checkpoints (open flow first)
}


class AdapterRegistry:
    def __init__(self, selection: dict[str, str] | None = None):
        self.selection = selection or {}  # role -> adapter name (from config)
        self._cache: dict[str, Adapter] = {}

    def get(self, name: str) -> Adapter:
        if name not in self._cache:
            if name not in _BUILTIN:
                raise KeyError(f"unknown adapter '{name}'")
            self._cache[name] = _BUILTIN[name]()
        return self._cache[name]

    def for_role(self, role: str, prefer: str | None = None) -> Adapter | None:
        candidates: list[str] = []
        if prefer:
            candidates.append(prefer)
        if role in self.selection:
            candidates.append(self.selection[role])
        candidates += _ROLE_DEFAULTS.get(role, [])
        # first available wins; else first known (so callers can report missing)
        first_known = None
        for name in candidates:
            if name not in _BUILTIN:
                continue
            adapter = self.get(name)
            if first_known is None:
                first_known = adapter
            if adapter.available():
                return adapter
        return first_known

    def role_report(self) -> dict[str, dict]:
        """For every role: which adapter :meth:`for_role` would actually pick
        right now (config pin honored, availability fallback applied), whether
        that pick is live, and the candidates that were considered."""
        out: dict[str, dict] = {}
        for role in _ROLE_DEFAULTS:
            candidates: list[str] = []
            if role in self.selection:
                candidates.append(self.selection[role])
            candidates += [c for c in _ROLE_DEFAULTS[role] if c not in candidates]
            a = self.for_role(role)
            out[role] = {
                "selected": a.name if a else None,
                "available": bool(a and a.available()),
                "pinned": self.selection.get(role),
                "candidates": candidates,
            }
        return out

    def all_manifests(self) -> list[dict]:
        out = []
        for name, cls in _BUILTIN.items():
            a = cls()
            man = a.manifest()
            out.append({
                "adapter": name, "available": a.available(),
                "version": a.version() if a.available() else "not-installed",
                "roles": man.roles, "languages": man.languages,
                "known_gaps": man.known_gaps})
        return out
