"""Normalized coverage model (SPEC §8.7, FR-COV-01/02).

One model spanning functional coverage (covergroups/coverpoints/bins/crosses) and
code coverage (line/toggle/branch/FSM), with weights, per-test attribution and a
mapping to testplan sections. Commercial tools feed it via UCIS export; Verilator
and cocotb-coverage feed it directly. Bins carry a source citation so
``cov.holes`` can point the agent at the covergroup definition.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bin:
    id: str  # e.g. "axi_mstr_cg::burst_cp::wrap16"
    group: str
    point: str
    name: str
    count: int = 0
    weight: int = 1
    source: str = ""  # file:line
    section: str = ""  # testplan section
    kind: str = "functional"  # functional | line | toggle | branch | fsm

    @property
    def hit(self) -> bool:
        return self.count > 0


@dataclass
class CoverageModel:
    bins: dict[str, Bin] = field(default_factory=dict)
    per_test: dict[str, set[str]] = field(default_factory=dict)  # test -> hit bin ids
    sources: list[str] = field(default_factory=list)  # provenance (job ids / files)

    def add_bin(self, b: Bin, test: str | None = None) -> None:
        existing = self.bins.get(b.id)
        if existing:
            existing.count += b.count
        else:
            self.bins[b.id] = b
        if test and b.count > 0:
            self.per_test.setdefault(test, set()).add(b.id)

    def merge(self, other: "CoverageModel") -> None:
        for b in other.bins.values():
            self.add_bin(Bin(**{**b.__dict__}))
        for test, ids in other.per_test.items():
            self.per_test.setdefault(test, set()).update(ids)
        self.sources.extend(other.sources)

    # ---- rollups ------------------------------------------------------------

    def _rate(self, bins: list[Bin]) -> float:
        tw = sum(b.weight for b in bins)
        hw = sum(b.weight for b in bins if b.hit)
        return (100.0 * hw / tw) if tw else 100.0

    def by_kind(self) -> dict[str, float]:
        out: dict[str, list[Bin]] = {}
        for b in self.bins.values():
            out.setdefault(b.kind, []).append(b)
        return {k: round(self._rate(v), 2) for k, v in out.items()}

    def by_section(self) -> dict[str, float]:
        out: dict[str, list[Bin]] = {}
        for b in self.bins.values():
            out.setdefault(b.section or "(unmapped)", []).append(b)
        return {k: round(self._rate(v), 2) for k, v in out.items()}

    def total(self) -> float:
        # An empty model is "no data", not "fully covered" — reporting 100%
        # here let a mis-dispatched coverage run read as green.
        if not self.bins:
            return 0.0
        return round(self._rate(list(self.bins.values())), 2)

    def to_dict(self) -> dict:
        return {
            "bins": {i: b.__dict__ for i, b in self.bins.items()},
            "per_test": {t: sorted(ids) for t, ids in self.per_test.items()},
            "sources": self.sources,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CoverageModel":
        m = cls(sources=d.get("sources", []))
        for i, bd in d.get("bins", {}).items():
            m.bins[i] = Bin(**bd)
        for t, ids in d.get("per_test", {}).items():
            m.per_test[t] = set(ids)
        return m
