"""Test history across runs (SPEC §8.5 FR-JOB-06): flake detection needs a past.

``regression.py`` clusters failures by normalized signature and its docstring
promises that "seed-stability history yields flake detection". The clustering is
real — but it was built inside one ``run()`` and discarded when that call
returned, so the platform could only ever see one night at a time.

Flake detection is *definitionally* cross-run: a test that fails on seed 7
tonight and passes on seed 7 tomorrow is flaky, and with per-run state that
observation is unreachable no matter how good the clustering is. This is the
store that makes the claim true.

What is recorded is deliberately small — outcome, signature, job id, when — so
a year of nightlies stays a file you can read. The signature does the heavy
lifting: it is the same normalization the failure clusters use, so "the same
failure" means the same thing here as it does there.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..util.jsonio import atomic_write, dump_json, load_json

# a test needs this many runs at one seed before "it passed once and failed
# once" is worth calling flaky rather than a fluke of ordering
MIN_RUNS_FOR_FLAKE = 2


class TestHistory:
    """Append-only outcomes per (test, seed), in ``.chipchamp/history.json``.

    Keyed by test AND seed because that is what makes the verdict falsifiable:
    two different seeds disagreeing is a test doing its job (different stimulus,
    different result); the SAME seed disagreeing is nondeterminism, and there is
    nowhere else for it to come from.
    """

    __test__ = False   # a class named Test* that pytest must not try to collect

    def __init__(self, dot: str | Path, limit_per_key: int = 40):
        self.path = Path(dot) / "history.json"
        self.limit = limit_per_key
        self._data: Optional[dict] = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                d = load_json(self.path)
                self._data = d if isinstance(d, dict) else {}
            except Exception:
                self._data = {}
        return self._data

    @staticmethod
    def key(test: str, seed) -> str:
        return f"{test}#{seed if seed is not None else '-'}"

    def record(self, test: str, seed, status: str, *, signature: str = "",
               job: str = "", save: bool = True) -> None:
        if not test:
            return
        runs = self._load().setdefault(self.key(test, seed), [])
        runs.append({"t": round(time.time(), 1), "status": status,
                     "sig": signature[:200], "job": job})
        del runs[:-self.limit]          # keep the tail; old nights add nothing
        if save:
            self.save()

    def save(self) -> None:
        try:
            atomic_write(self.path, dump_json(self._load(), sort_keys=True))
        except OSError:
            pass    # history is an aid, never a reason to fail a job

    # ---- reading ------------------------------------------------------------

    def runs(self, test: str, seed=None) -> list[dict]:
        return list(self._load().get(self.key(test, seed), []))

    def verdict(self, test: str, seed=None) -> dict:
        """`stable` / `flaky` / `failing` / `unknown` for one (test, seed).

        `flaky` requires disagreement at the SAME seed. A test that fails at
        seed 3 and passes at seed 4 is not flaky — it found a bug at seed 3,
        and calling that flaky is how real failures get ignored."""
        runs = self.runs(test, seed)
        if len(runs) < MIN_RUNS_FOR_FLAKE:
            return {"verdict": "unknown", "runs": len(runs)}
        outcomes = {r["status"] for r in runs}
        passed = sum(1 for r in runs if r["status"] == "passed")
        out = {"runs": len(runs), "passed": passed,
               "failed": len(runs) - passed,
               "last": runs[-1]["status"]}
        if outcomes <= {"passed"}:
            out["verdict"] = "stable"
        elif "passed" in outcomes:
            out["verdict"] = "flaky"
            out["flake_rate"] = round((len(runs) - passed) / len(runs), 2)
            # distinct signatures matter: one recurring failure is a different
            # animal from a test that breaks a new way every night
            out["signatures"] = sorted({r["sig"] for r in runs
                                        if r["status"] != "passed" and r["sig"]})
        else:
            out["verdict"] = "failing"
        return out

    def flaky_tests(self) -> list[dict]:
        """Every (test, seed) whose own history contradicts itself."""
        out = []
        for key in sorted(self._load()):
            test, _, seed = key.rpartition("#")
            v = self.verdict(test, None if seed == "-" else _int(seed))
            if v.get("verdict") == "flaky":
                out.append({"test": test, "seed": seed, **v})
        return sorted(out, key=lambda r: -r.get("flake_rate", 0))

    def summary(self) -> dict:
        keys = self._load()
        verdicts: dict[str, int] = {}
        for key in keys:
            test, _, seed = key.rpartition("#")
            v = self.verdict(test, None if seed == "-" else _int(seed))["verdict"]
            verdicts[v] = verdicts.get(v, 0) + 1
        return {"tracked": len(keys), "verdicts": verdicts,
                "flaky": self.flaky_tests()[:10]}


def _int(s: str):
    try:
        return int(s)
    except (TypeError, ValueError):
        return s
