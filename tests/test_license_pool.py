"""License seats: a job that cannot get one does not run, and never releases
a seat it did not take; features are acquired in one order everywhere."""
from __future__ import annotations

import threading

from chipchamp.adapters.base import NormalizedDiagnostic, Plan, Step, ToolResult
from chipchamp.jobs.runner import JobRunner, LicensePool


def test_failed_acquire_gives_back_the_partial_set():
    pool = LicensePool({"a": 1, "b": 1})
    assert pool.acquire(["a"])                       # someone holds a
    assert pool.acquire(["b", "a"], timeout=0.05) is False
    assert pool.acquire(["b"], timeout=0)            # b was not left taken


def test_features_are_taken_in_sorted_order():
    pool = LicensePool({"x": 1, "y": 1})
    order = []
    for name, sem in pool._sem.items():
        real = sem.acquire
        sem.acquire = (lambda n=name, r=real: (lambda timeout=None: (order.append(n), r(timeout=timeout))[1]))()
    assert pool.acquire(["y", "x"])
    assert order == ["x", "y"]


def test_opposite_orders_do_not_deadlock():
    pool = LicensePool({"a": 1, "b": 1})
    done = []

    def worker(feats):
        for _ in range(20):
            assert pool.acquire(feats, timeout=5)
            pool.release(feats)
        done.append(feats)

    ts = [threading.Thread(target=worker, args=(f,)) for f in (["a", "b"], ["b", "a"])]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert len(done) == 2


class _Adapter:
    name = "fake"
    parsed = 0

    def version(self):
        return "1"

    def available(self):
        return True

    def parse(self, plan, results):
        self.parsed += 1
        return ToolResult(ok=True, kind="lint", adapter=self.name, status="passed",
                          summary="ran", diagnostics=[NormalizedDiagnostic(
                              tool="fake", severity="info", message="m", file="a.sv", line=1)])


def test_no_seat_means_no_run_and_no_phantom_seat(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None, licenses={"q": 1},
                  license_wait_s=0.1)
    assert r.licenses.acquire(["q"])                 # the one seat is taken elsewhere
    ad = _Adapter()
    plan = Plan(kind="lint", adapter="fake", workdir=str(tmp_path),
                steps=[Step(argv=["true"], cwd=str(tmp_path))], license_features=["q"])
    rec, res = r.submit(plan, ad, input_files=[], no_cache=True)
    assert rec.status == "error" and "no license seat" in rec.summary
    assert res.ok is False and ad.parsed == 0        # never ran, never parsed
    assert r.get(rec.id).status == "error"           # persisted as an outcome
    # the pool still has exactly ONE seat: the failed job released nothing
    assert r.licenses.acquire(["q"], timeout=0) is False
    r.licenses.release(["q"])
    assert r.licenses.acquire(["q"], timeout=0) is True
    assert r.licenses.acquire(["q"], timeout=0) is False


def test_with_a_seat_the_job_runs_and_releases_it(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None, licenses={"q": 1})
    ad = _Adapter()
    plan = Plan(kind="lint", adapter="fake", workdir=str(tmp_path),
                steps=[Step(argv=["true"], cwd=str(tmp_path))], license_features=["q"])
    rec, _ = r.submit(plan, ad, input_files=[], no_cache=True)
    assert rec.status == "passed" and ad.parsed == 1
    assert r.licenses.acquire(["q"], timeout=0)      # seat is back
