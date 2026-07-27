"""Waveform query service (SPEC §8.6): the agent's eyes on a simulation.

Exposes signal-level queries over a parsed dump — value-at-time, transitions in a
window, first-time-expression-true, run-vs-run first divergence, and X-origin
tracing — so the agent reasons over slices, never a raw multi-GB dump
(FR-WAVE-02..05). Results are bounded and provenance-tagged (SPEC §8.9 L4).
"""
from __future__ import annotations

import bisect
import fnmatch
import os
from dataclasses import dataclass
from typing import Optional

from .expr import SignalExpr
from .vcd import VcdData, parse_vcd


def _fst_to_vcd(fst_path: str) -> str:
    """Convert FST → VCD via gtkwave's fst2vcd (cached beside the FST)."""
    import shutil
    import subprocess
    out = fst_path[:-4] + ".chipchamp.vcd"
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(fst_path):
        return out
    if shutil.which("fst2vcd") is None:
        raise RuntimeError("FST support needs `fst2vcd` (gtkwave/oss-cad-suite) on PATH")
    with open(out, "w") as fh:
        subprocess.run(["fst2vcd", fst_path], stdout=fh, check=True, timeout=300)
    return out


def _to_int(val: str) -> Optional[int]:
    if val is None:
        return None
    if any(c in "xz" for c in val):
        return None
    try:
        return int(val, 2) if len(val) > 1 or val in ("0", "1") else int(val)
    except ValueError:
        return None


def _is_x(val: Optional[str]) -> bool:
    return val is None or any(c in "xz" for c in val)


@dataclass
class Divergence:
    signal: str
    time: int
    value_a: str
    value_b: str


class WaveStore:
    def __init__(self, data: VcdData, provenance: str = ""):
        self.data = data
        self.provenance = provenance
        self._times: dict[str, list[int]] = {
            code: [t for t, _ in ch] for code, ch in data.changes.items()}

    @classmethod
    def open(cls, path: str, provenance: str = "") -> "WaveStore":
        """Open a dump. FST (FR-WAVE-01) is supported via the gtkwave `fst2vcd`
        bridge; VCD parses natively. FSDB requires a Verdi-licensed reader (M2)."""
        if path.endswith(".fst"):
            path = _fst_to_vcd(path)
        return cls(parse_vcd(path), provenance or path)

    # ---- metadata -----------------------------------------------------------

    def info(self) -> dict:
        return {"time_range": [0, self.data.end_time], "timescale": self.data.timescale,
                "signal_count": len(self.data.signals),
                "scopes": sorted(self.data.scopes)[:50],
                "provenance": self.provenance}

    def find_signals(self, pattern: str, limit: int = 50) -> list[str]:
        pats = pattern if "*" in pattern or "?" in pattern else f"*{pattern}*"
        out = [p for p in sorted(self.data.by_path) if fnmatch.fnmatch(p, pats)]
        return out[:limit]

    # ---- point/window queries ----------------------------------------------

    def value(self, path: str, t: int) -> Optional[str]:
        code = self.data.id_for(path)
        if code is None:
            return None
        times = self._times.get(code, [])
        if not times:
            return None
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            return None
        return self.data.changes[code][i][1]

    def changes(self, path: str, t0: int, t1: int, max_count: int = 100) -> dict:
        code = self.data.id_for(path)
        if code is None:
            return {"error": f"signal not found: {path}"}
        ch = self.data.changes[code]
        out = [(t, v) for t, v in ch if t0 <= t <= t1]
        truncated = len(out) > max_count
        return {"signal": path, "transitions": out[:max_count],
                "truncated": truncated, "provenance": self.provenance}

    def when(self, expr: str, from_t: int = 0, direction: str = "forward",
             occurrence: int = 1) -> dict:
        se = SignalExpr(expr)
        codes = {name: self.data.id_for(name) for name in se.names}
        missing = [n for n, c in codes.items() if c is None
                   and not n.replace(".", "").isidentifier() is False]
        real_missing = [n for n, c in codes.items() if c is None]
        # candidate times = union of change times of referenced signals
        cand: set[int] = {0}
        for name, code in codes.items():
            if code:
                cand.update(t for t in self._times.get(code, []) if t >= 0)
        cand_sorted = sorted(cand)
        if direction == "backward":
            cand_sorted = [t for t in cand_sorted if t <= from_t][::-1]
        else:
            cand_sorted = [t for t in cand_sorted if t >= from_t]
        hits = 0
        for t in cand_sorted:
            def value_of(name: str, _t=t):
                v = self.value(name, _t)
                return _to_int(v) if v is not None else None

            def isx_of(name: str, _t=t):
                return _is_x(self.value(name, _t))

            res = se.eval(value_of, isx_of)
            if res:
                hits += 1
                if hits >= occurrence:
                    vals = {n: self.value(n, t) for n in se.names if codes.get(n)}
                    return {"time": t, "values": vals, "expr": expr,
                            "missing_signals": real_missing,
                            "provenance": self.provenance}
        return {"time": None, "expr": expr, "missing_signals": real_missing,
                "note": "expression never held in the searched range",
                "provenance": self.provenance}

    def compare(self, other: "WaveStore", scope: str = "", window: Optional[tuple[int, int]] = None,
                max_signals: int = 2000) -> dict:
        """First-divergence between two runs over a scope (SPEC FR-WAVE-04, P1)."""
        t0, t1 = window or (0, max(self.data.end_time, other.data.end_time))
        common = []
        for path in self.data.by_path:
            if scope and not (path.startswith(scope + ".") or path == scope
                              or ("." + scope + ".") in ("." + path)):
                # loose scope match on suffix
                if scope not in path:
                    continue
            if other.data.id_for(path) is not None:
                common.append(path)
            if len(common) >= max_signals:
                break
        first: Optional[Divergence] = None
        # candidate times: union of all change times across compared signals
        cand: set[int] = set()
        for path in common:
            a = self.data.id_for(path)
            b = other.data.id_for(path)
            cand.update(t for t in self._times.get(a, []) if t0 <= t <= t1)
            cand.update(t for t in other._times.get(b, []) if t0 <= t <= t1)
        for t in sorted(cand):
            for path in common:
                va, vb = self.value(path, t), other.value(path, t)
                if va != vb:
                    d = Divergence(signal=path, time=t, value_a=str(va), value_b=str(vb))
                    if first is None or t < first.time:
                        first = d
                    break
            if first is not None:
                break
        return {"first_divergence": first.__dict__ if first else None,
                "signals_compared": len(common),
                "provenance_a": self.provenance, "provenance_b": other.provenance}

    def trace_x(self, path: str, from_t: Optional[int] = None) -> dict:
        """Earliest time `path` is X/Z (the seed for cone-based X-origin tracing
        combined with the design DB in the wave.trace_x tool)."""
        code = self.data.id_for(path)
        if code is None:
            return {"error": f"signal not found: {path}"}
        for t, v in self.data.changes[code]:
            if from_t is not None and t < from_t:
                continue
            if _is_x(v):
                return {"signal": path, "first_x_time": t, "value": v,
                        "provenance": self.provenance}
        return {"signal": path, "first_x_time": None,
                "note": "signal never X/Z in dump", "provenance": self.provenance}

    def snapshot(self, paths: list[str], t0: int, t1: int, max_edges: int = 40) -> dict:
        """Return the transition data needed to render a timing excerpt."""
        out = {}
        for p in paths:
            code = self.data.id_for(p)
            if code is None:
                continue
            edges = [(t, v) for t, v in self.data.changes[code] if t0 <= t <= t1]
            out[p] = {"width": self.data.signals[code].width,
                      "edges": edges[:max_edges],
                      "value_at_start": self.value(p, t0)}
        return {"window": [t0, t1], "signals": out, "provenance": self.provenance}
