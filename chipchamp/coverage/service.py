"""Coverage service (SPEC §8.7): summary, holes, attribution, merge, baseline gate.

Backs the ``cov.*`` tools and playbook P5. The baseline gate (FR-COV-05) fails any
change that silently lowers total or per-section coverage — the anti-gaming spine
that stops "closure by exclusion".
"""
from __future__ import annotations

from dataclasses import dataclass

from ..util.jsonio import atomic_write, dump_json, load_json
from .model import CoverageModel


@dataclass
class BaselineVerdict:
    ok: bool
    total_delta: float
    regressed_sections: list[dict]
    summary: str


class CoverageService:
    def __init__(self, model: CoverageModel | None = None):
        self.model = model or CoverageModel()

    def summary(self) -> dict:
        return {"total": self.model.total(), "by_kind": self.model.by_kind(),
                "by_section": self.model.by_section(),
                "bins": len(self.model.bins),
                "tests": sorted(self.model.per_test)}

    def holes(self, scope: str = "", kind: str = "functional", limit: int = 50) -> dict:
        holes = []
        for b in self.model.bins.values():
            if b.hit:
                continue
            if kind not in ("both", "all") and b.kind != kind and not (
                    kind == "code" and b.kind in ("line", "toggle", "branch")):
                continue
            if scope and scope not in b.id and scope not in b.source and scope not in b.point:
                continue
            holes.append({"bin": b.id, "source": b.source, "weight": b.weight,
                          "section": b.section,
                          "nearest_test": self._nearest_test(b.id)})
        total = len(holes)
        return {"holes": holes[:limit], "total_holes": total,
                "truncated": total > limit}

    def _nearest_test(self, bin_id: str) -> dict | None:
        """The test hitting the most sibling bins (same group::point) — the P5
        'closest existing stimulus' heuristic."""
        prefix = "::".join(bin_id.split("::")[:2])
        best, best_n = None, 0
        for test, ids in self.model.per_test.items():
            n = sum(1 for i in ids if i.startswith(prefix))
            if n > best_n:
                best, best_n = test, n
        return {"test": best, "sibling_hits": best_n} if best else None

    def attribution(self, bin_id: str | None = None, test: str | None = None) -> dict:
        if bin_id:
            tests = [t for t, ids in self.model.per_test.items() if bin_id in ids]
            return {"bin": bin_id, "hit_by": tests}
        if test:
            return {"test": test, "bins": sorted(self.model.per_test.get(test, set()))}
        return {"error": "provide bin_id or test"}

    def merge(self, other: CoverageModel) -> None:
        self.model.merge(other)

    def baseline_check(self, baseline: CoverageModel, tol: float = 0.0) -> BaselineVerdict:
        """FR-COV-05: fail if total or any section coverage regressed."""
        base_svc = CoverageService(baseline)
        cur_total = self.model.total()
        base_total = baseline.total()
        regressed = []
        cur_sections = self.model.by_section()
        base_sections = baseline.by_section()
        for sec, base_rate in base_sections.items():
            cur_rate = cur_sections.get(sec, 0.0)
            if cur_rate + tol < base_rate:
                regressed.append({"section": sec, "from": base_rate, "to": cur_rate})
        ok = (cur_total + tol >= base_total) and not regressed
        return BaselineVerdict(
            ok=ok, total_delta=round(cur_total - base_total, 2),
            regressed_sections=regressed,
            summary=(f"total {base_total}%→{cur_total}% "
                     f"({'+' if cur_total>=base_total else ''}{round(cur_total-base_total,2)}%), "
                     f"{len(regressed)} section(s) regressed"))

    # ---- persistence --------------------------------------------------------

    def save(self, path: str) -> str:
        atomic_write(path, dump_json(self.model.to_dict()))
        return path

    @classmethod
    def load(cls, path: str) -> "CoverageService":
        return cls(CoverageModel.from_dict(load_json(path)))
