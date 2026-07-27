"""Regression manager with failure-signature clustering (SPEC §8.5).

Runs a manifest/tag/affected set of tests (optionally sweeping seeds), bounded by
farm/license capacity, then turns N raw failures into a handful of *problems* by
normalizing failure messages (strip times, seeds, addresses, counts), hashing and
bucketing (FR-JOB-05). Seed-stability history yields flake detection (FR-JOB-06).
This is the engine behind playbook P1 (triage).
"""
from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from .record import JobRecord

_NORMALIZERS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"@\s*\d+\s*(ns|ps|us)?"), "@TIME"),
    (re.compile(r"\bseed[=: ]+\d+", re.I), "seed=SEED"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalize_signature(text: str) -> str:
    s = text.strip()
    for rx, repl in _NORMALIZERS:
        s = rx.sub(repl, s)
    return s.strip()


@dataclass
class FailureCluster:
    id: str
    signature: str
    count: int = 0
    representative: Optional[dict] = None  # {test, seed, job, waves}
    members: list[str] = field(default_factory=list)  # job ids
    tests: set[str] = field(default_factory=set)


@dataclass
class RegressionResult:
    run_id: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    jobs: list[JobRecord] = field(default_factory=list)
    clusters: list[FailureCluster] = field(default_factory=list)
    flaky: list[dict] = field(default_factory=list)  # {test, pass_seeds, fail_seeds}
    errored_tests: list[dict] = field(default_factory=list)  # {test, seed, job, reason}

    def summary(self) -> str:
        return (f"{self.run_id}: {self.passed}/{self.total} passed, "
                f"{self.failed} failed, {self.errored} errored, "
                f"{len(self.clusters)} failure cluster(s), {len(self.flaky)} flaky")


class RegressionManager:
    def __init__(self, run_sim: Callable[[str, int], JobRecord], max_parallel: int = 4):
        """`run_sim(test_name, seed) -> JobRecord` submits one sim via the runner."""
        self.run_sim = run_sim
        self.max_parallel = max(1, max_parallel)

    def run(self, tests: list[str], seeds: Optional[list[int]] = None,
            run_id: str = "reg") -> RegressionResult:
        seeds = seeds or [0]
        work = [(t, s) for t in tests for s in seeds]
        jobs: list[tuple[str, int, JobRecord]] = []
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futs = {pool.submit(self.run_sim, t, s): (t, s) for t, s in work}
            for fut in futs:
                t, s = futs[fut]
                try:
                    rec = fut.result()
                    jobs.append((t, s, rec))
                except Exception as e:  # a crashed job must not abort the regression
                    jobs.append((t, s, _error_record(t, s, str(e))))

        res = RegressionResult(run_id=run_id, total=len(jobs))
        by_test_seed_status: dict[str, dict[int, str]] = defaultdict(dict)
        clusters: dict[str, FailureCluster] = {}
        for t, s, rec in jobs:
            by_test_seed_status[t][s] = rec.status
            res.jobs.append(rec)
            if rec.status == "passed":
                res.passed += 1
            elif rec.status == "error":
                res.errored += 1
                res.errored_tests.append({"test": t, "seed": s, "job": rec.id,
                                          "reason": rec.summary[:160]})
            else:
                res.failed += 1
                sig = normalize_signature(_failure_text(rec))
                key = sig
                cl = clusters.get(key)
                if cl is None:
                    cl = FailureCluster(id=f"C{len(clusters)+1}", signature=sig)
                    clusters[key] = cl
                cl.count += 1
                cl.members.append(rec.id)
                cl.tests.add(t)
                if cl.representative is None:
                    cl.representative = {"test": t, "seed": s, "job": rec.id,
                                         "waves": "waves" in rec.artifacts}
        res.clusters = sorted(clusters.values(), key=lambda c: -c.count)

        # flake: a test that both passes and fails across seeds
        for t, seed_status in by_test_seed_status.items():
            statuses = set(seed_status.values())
            if "passed" in statuses and statuses & {"failed", "timeout"}:
                res.flaky.append({
                    "test": t,
                    "pass_seeds": [s for s, st in seed_status.items() if st == "passed"],
                    "fail_seeds": [s for s, st in seed_status.items() if st != "passed"]})
        return res


def _failure_text(rec: JobRecord) -> str:
    parts = [rec.summary]
    for d in rec.result.get("diagnostics", []):
        if d.get("category") == "sim" or d.get("severity") == "error":
            parts.append(d.get("message", ""))
    return " | ".join(p for p in parts if p) or rec.status


def _error_record(test: str, seed: int, msg: str) -> JobRecord:
    r = JobRecord(id=f"ERR-{test}-{seed}", kind="sim", adapter="?",
                  status="error", summary=msg, seed=seed)
    return r
