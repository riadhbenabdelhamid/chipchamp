"""Durable regression checkpoints. Saved state contains identities, never commands."""
from __future__ import annotations

import fcntl
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from .cache import cache_key
from .env import scrub_env
from .inputs import snapshot_current
from ..util.hashing import hash_manifest, sha256_file, sha256_text
from ..util.jsonio import atomic_write, dump_json, load_json


def case_key(test, seed):
    return sha256_text(dump_json([test, seed]))[:24]


@contextmanager
def run_lock(directory):
    """Reject concurrent writers to the same run; an exited process releases it."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    with open(Path(directory) / ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("this regression is already running; wait before resuming it") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_checkpoint(path, ctx):
    data = load_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported regression checkpoint")
    if data.get("target") != ctx.target_name or data.get("workspace") != os.path.realpath(ctx.ws.root):
        raise ValueError("checkpoint belongs to a different workspace or target")
    tests, seeds = data.get("tests"), data.get("seeds")
    if (not isinstance(tests, list) or not tests
            or any(not isinstance(t, str) or not t for t in tests) or len(set(tests)) != len(tests)
            or not isinstance(seeds, list) or not seeds
            or any(type(s) is not int for s in seeds) or len(set(seeds)) != len(seeds)
            or not isinstance(data.get("cases"), dict)
            or not isinstance(data.get("charged_jobs", []), list)
            or not isinstance(data.get("run_id"), str)):
        raise ValueError("invalid regression checkpoint matrix")
    if any(not isinstance(j, str) or not re.fullmatch(r"J-[\w-]+", j)
           for j in data.get("charged_jobs", [])):
        raise ValueError("invalid charged job identity in checkpoint")
    for entry in data["cases"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("job"), str):
            raise ValueError("invalid checkpoint case")
        if entry.get("attempt") and not re.fullmatch(r"[0-9a-f]{32}", str(entry["attempt"])):
            raise ValueError("invalid checkpoint attempt")
    return data


class Checkpoint:
    def __init__(self, path, ctx, tests, seeds, run_id, plan=None, previous=None):
        self.path = Path(path)
        self.ctx = ctx
        self._lock = threading.RLock()
        self.data = previous or {
            "schema_version": 1, "workspace": os.path.realpath(ctx.ws.root),
            "target": ctx.target_name, "run_id": run_id, "tests": tests, "seeds": seeds,
            "plan": plan, "cases": {}, "charged_jobs": [], "state": "running"}

    def save(self):
        with self._lock:
            atomic_write(self.path, dump_json(self.data))

    def start(self, test, seed, attempt):
        with self._lock:
            self.data["cases"][case_key(test, seed)] = {
                "test": test, "seed": seed, "job": "", "status": "running", "attempt": attempt}
            self.save()

    def record(self, test, seed, rec):
        with self._lock:
            self.data["cases"].setdefault(case_key(test, seed), {}).update(
                test=test, seed=seed, job=rec.id, status=rec.status)
            if rec.id in self.ctx._charged_jobs and rec.id not in self.data["charged_jobs"]:
                self.data["charged_jobs"].append(rec.id)
            self.save()

    def restore_costs(self):
        # A fresh CLI context must not give a partially completed run a fresh
        # budget. Within a context the existing ledger has already charged it.
        # Runner-owned provenance also covers a job that finished just before
        # the parent died, before its checkpoint callback could record it.
        known = {r.id for r in self.ctx.runner.list_jobs()
                 if r.regression_id == self.path.parent.name}
        for job_id in sorted(known | set(self.data["charged_jobs"])):
            if not isinstance(job_id, str) or not re.fullmatch(r"J-[\w-]+", job_id):
                raise ValueError("invalid charged job identity in checkpoint")
            if job_id in self.ctx._charged_jobs:
                continue
            rec = self.ctx.runner.get(job_id)
            self.ctx.ledger.record_v3()
            if rec:
                self.ctx.ledger.record_job(rec)
            self.ctx._charged_jobs.add(job_id)

    def reusable_case(self, test, seed, adapter, plan, files):
        """Trust the runner's record, fingerprints and evidence, never a JSON verdict."""
        entry = self.data["cases"].get(case_key(test, seed), {})
        job_id = entry.get("job", "")
        if not re.fullmatch(r"J-[\w-]+", job_id):
            return None
        rec = self.ctx.runner.get(job_id)
        if not (rec and rec.status == "passed" and rec.test_name == test
                and rec.seed == seed and rec.target_name == self.ctx.target_name
                and rec.execution_key and rec.input_snapshot.get("complete")
                and rec.steps and rec.evidence_hashes and snapshot_current(rec.input_snapshot)):
            return None
        key = cache_key(plan, adapter, inputs_hash=hash_manifest(files), seed=seed,
                        input_files=files,
                        env_modules=self.ctx.ws.config.get("tools", {}).get(
                            "env_modules", {}).get(adapter.name, []),
                        environment=scrub_env(allowlist=self.ctx.runner.env_allowlist,
                                              deny=self.ctx.runner.env_deny))
        if key != rec.execution_key:
            return None
        required = {*(s.log_path for s in rec.steps), *rec.artifacts.values()}
        if required - rec.evidence_hashes.keys():
            return None
        try:
            if any(sha256_file(p) != digest for p, digest in rec.evidence_hashes.items()):
                return None
        except OSError:
            return None
        rec.cached = True
        if not any(r.id == rec.id for r in self.ctx.task_jobs):
            self.ctx.task_jobs.append(rec)
        return rec
