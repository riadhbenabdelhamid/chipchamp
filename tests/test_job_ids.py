"""Job ids are claimed, not counted: two processes on one store never share
an id, a corrupt seq.txt never restarts numbering over old jobs, and a cache
hit must be the same kind, adapter and inputs as the job about to run."""
from __future__ import annotations

import json
import os

from chipchamp.adapters.base import NormalizedDiagnostic, Plan, Step, ToolResult
from chipchamp.jobs.cache import reusable
from chipchamp.jobs.record import JobRecord
from chipchamp.jobs.runner import JobRunner


def test_two_processes_on_one_store_never_collide(tmp_path):
    store = str(tmp_path / "runs")
    a, b = JobRunner(store, registry=None), JobRunner(store, registry=None)
    ids = [a._next_id(), b._next_id(), a._next_id(), b._next_id()]
    assert ids == ["J-0001", "J-0002", "J-0003", "J-0004"]
    assert all((tmp_path / "runs" / i).is_dir() for i in ids)
    assert (tmp_path / "runs" / "seq.txt").read_text() == "4"


def test_corrupt_seq_never_restarts_over_existing_jobs(tmp_path):
    store = tmp_path / "runs"
    (store / "J-0005").mkdir(parents=True)
    (store / "seq.txt").write_text("")            # crash mid-write
    assert JobRunner(str(store), registry=None)._next_id() == "J-0006"
    (store / "seq.txt").write_text("garbage")
    assert JobRunner(str(store), registry=None)._next_id() == "J-0007"


def test_a_directory_claimed_by_someone_else_is_skipped(tmp_path):
    store = tmp_path / "runs"
    r = JobRunner(str(store), registry=None)
    assert r._next_id() == "J-0001"
    os.mkdir(store / "J-0002")                    # another process got there first
    assert r._next_id() == "J-0003"


# ---- cache hits must be the same job -----------------------------------------

def _rec(tmp_path, **kw):
    d = tmp_path / "runs" / "J-0001"
    d.mkdir(parents=True, exist_ok=True)
    base = dict(id="J-0001", kind="lint", adapter="fake", status="passed",
                input_files=["a.sv"], inputs_hash="h1",
                result={"ok": True, "status": "passed"})
    base.update(kw)
    return JobRecord(**base), d


def test_reusable_rejects_a_record_of_another_kind_adapter_or_inputs(tmp_path):
    rec, d = _rec(tmp_path)
    lint = Plan(kind="lint", adapter="fake", workdir=".", steps=[])
    sim = Plan(kind="sim", adapter="fake", workdir=".", steps=[])
    assert reusable(rec, d, lint, inputs_hash="h1", adapter="fake")
    assert not reusable(rec, d, sim, inputs_hash="h1", adapter="fake")
    assert not reusable(rec, d, lint, inputs_hash="h2", adapter="fake")
    assert not reusable(rec, d, lint, inputs_hash="h1", adapter="verible")


class _Adapter:
    name = "fake"

    def __init__(self):
        self.parsed = 0

    def version(self):
        return "1"

    def available(self):
        return True

    def parse(self, plan, results):
        self.parsed += 1
        return ToolResult(ok=True, kind="lint", adapter=self.name, status="passed",
                          summary=f"parse #{self.parsed}", diagnostics=[
                              NormalizedDiagnostic(tool="fake", severity="info",
                                                   message="m", file="a.sv", line=1)],
                          metrics={"errors": 0})


def test_a_stale_index_entry_pointing_at_the_wrong_job_is_not_served(tmp_path):
    src = tmp_path / "a.sv"
    src.write_text("module a; endmodule\n")
    r, ad = JobRunner(str(tmp_path / "runs"), registry=None), _Adapter()
    plan = Plan(kind="lint", adapter="fake", workdir=str(tmp_path),
                steps=[Step(argv=["true"], cwd=str(tmp_path))])
    rec1, _ = r.submit(plan, ad, input_files=[str(src)])
    rec2, _ = r.submit(plan, ad, input_files=[str(src)])
    assert rec2.cached and ad.parsed == 1            # the genuine hit still works
    # corrupt the record the index points at: it now claims to be a sim
    rp = tmp_path / "runs" / rec1.id / "record.json"
    data = json.loads(rp.read_text())
    data["kind"] = "sim"
    rp.write_text(json.dumps(data))
    rec3, _ = r.submit(plan, ad, input_files=[str(src)])
    assert not rec3.cached and rec3.id != rec1.id and ad.parsed == 2


# ---- the cache key knows which code produced the result ----------------------

def test_cache_key_changes_when_the_adapter_code_changes(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from chipchamp.jobs import cache as cache_mod
    src = tmp_path / "myadapter.py"
    src.write_text("class Ad:\n    name = 'ad'\n    def version(self): return '1'\n")
    spec = importlib.util.spec_from_file_location("myadapter", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["myadapter"] = mod
    spec.loader.exec_module(mod)
    ad = mod.Ad()
    plan = Plan(kind="lint", adapter="ad", workdir=str(tmp_path), steps=[])
    k1 = cache_mod.cache_key(plan, ad, inputs_hash="h")
    assert cache_mod.cache_key(plan, ad, inputs_hash="h") == k1      # stable
    # the parser changed: same binary version, same inputs, different code
    src.write_text(src.read_text() + "    # parse() now reads a new field\n")
    cache_mod._FINGERPRINTS.clear()
    assert cache_mod.cache_key(plan, ad, inputs_hash="h") != k1


def test_cache_key_changes_with_the_package_version(tmp_path, monkeypatch):
    import chipchamp
    from chipchamp.jobs import cache as cache_mod
    ad = _Adapter()
    plan = Plan(kind="lint", adapter="fake", workdir=str(tmp_path), steps=[])
    k1 = cache_mod.cache_key(plan, ad, inputs_hash="h")
    monkeypatch.setattr(chipchamp, "__version__", "9.9.9")
    cache_mod._FINGERPRINTS.clear()
    assert cache_mod.cache_key(plan, ad, inputs_hash="h") != k1
