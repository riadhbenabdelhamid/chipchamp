"""Job runner + store (SPEC §8.5).

Executes an adapter :class:`Plan` locally, capturing per-step logs, provenance
and cost into a :class:`JobRecord` under ``.chipchamp/runs/<id>/``. License-aware
concurrency is modeled with per-feature semaphores (FR-JOB-03) so a subagent
fan-out cannot check out more licensed seats than the pool allows. A future
LSF/SLURM runner (SPEC M2) implements the same interface; the agent core is
unaware of which runner backs a job (FR-JOB-02).

``submit`` runs synchronously and returns the finished record — right for the
seconds-long rungs an agent iterates on. ``submit_async`` is for the rungs that
are not: a regression takes hours, and holding a model connection (and a human)
open across one is the loop mismatch this platform exists to fix. It returns a
``running`` record immediately; ``wait`` blocks on it, ``poll`` does not, and
the record is persisted at both ends so a detached job survives the process that
launched it — which is what makes "come back to it" possible at all.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from ..adapters.base import Adapter, Plan, StepResult, ToolResult
from ..util.hashing import hash_manifest
from ..util.ids import new_job_id
from ..util.jsonio import atomic_write, dump_json, load_json
from .record import JobRecord, StepLog


class LicensePool:
    """Token semaphores per license feature (SPEC §8.5 FR-JOB-03)."""

    def __init__(self, pools: dict[str, int] | None = None):
        self._sem = {feat: threading.BoundedSemaphore(max(1, n))
                     for feat, n in (pools or {}).items()}
        self._counts = dict(pools or {})

    def acquire(self, features: list[str], timeout: float | None = None) -> bool:
        acquired = []
        for f in features:
            if f in self._sem:
                if not self._sem[f].acquire(timeout=timeout):
                    for a in acquired:
                        self._sem[a].release()
                    return False
                acquired.append(f)
        return True

    def release(self, features: list[str]) -> None:
        for f in features:
            if f in self._sem:
                try:
                    self._sem[f].release()
                except ValueError:
                    pass


class JobRunner:
    def __init__(self, store_dir: str, registry, *, env_modules: dict | None = None,
                 licenses: dict | None = None, env_allowlist: list[str] | None = None,
                 farm=None, cache: bool = True):
        from .cache import JobCache
        from .farm import FarmConfig
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.env_modules = env_modules or {}
        self.licenses = LicensePool({f: v.get("tokens", 1) if isinstance(v, dict) else v
                                     for f, v in (licenses or {}).items()})
        self.env_allowlist = env_allowlist or ["PATH", "HOME", "USER"]
        self.farm = farm or FarmConfig()
        self.cache = JobCache(self.store_dir, enabled=cache)
        self._seq = self._load_seq()
        self._lock = threading.Lock()
        self._async: dict[str, threading.Thread] = {}

    def _load_seq(self) -> int:
        f = self.store_dir / "seq.txt"
        if f.exists():
            try:
                return int(f.read_text().strip())
            except ValueError:
                return 0
        return 0

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            # the store dir can vanish mid-process (an experiment harness
            # sequestering the archive, a cleanup script) — id allocation is
            # the first write of every submit, so it must not be the thing
            # that turns a missing directory into a failed job
            self.store_dir.mkdir(parents=True, exist_ok=True)
            (self.store_dir / "seq.txt").write_text(str(self._seq))
            return new_job_id(self._seq)

    def submit(self, plan: Plan, adapter: Adapter, *,
               input_files: Optional[list[str]] = None,
               defines: Optional[dict] = None, seed: Optional[int] = None,
               timeout: Optional[float] = None,
               env_modules: Optional[list[str]] = None,
               no_cache: bool = False) -> tuple[JobRecord, ToolResult]:
        from .cache import cache_key, result_from_record, reusable
        key = ""
        if self.cache.enabled and not no_cache:
            try:
                key = cache_key(plan, adapter,
                                inputs_hash=hash_manifest(input_files or []),
                                seed=seed, defines=defines,
                                env_modules=env_modules)
            except Exception:
                key = ""   # an unhashable plan just means no reuse, never a fail
            hit = self.cache.lookup(key) if key else None
            if hit:
                rec = self.get(hit)
                if rec is not None and reusable(rec, self.store_dir / hit, plan):
                    result = result_from_record(rec)
                    if result is not None:
                        # the ORIGINAL record is returned, id and logs intact —
                        # the evidence a gate validates must be the real job,
                        # not a copy that claims to have run
                        rec.cached = True
                        return rec, result
        job_id = self._next_id()
        job_dir = self.store_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        rec = JobRecord(
            id=job_id, kind=plan.kind, adapter=adapter.name,
            tool_version=adapter.version(), status="running",
            submit_ts=time.time(), input_files=list(input_files or []),
            defines=defines or {}, seed=seed,
            license_features=list(plan.license_features),
            env_modules=env_modules or [])
        rec.compute_inputs_hash()
        rec.start_ts = time.time()

        got_license = self.licenses.acquire(rec.license_features, timeout=timeout)
        results: list[StepResult] = []
        try:
            for idx, st in enumerate(plan.steps):
                sr = self._run_step(st, job_dir, idx, timeout,
                                    env_modules=rec.env_modules)
                results.append(sr)
                rec.steps.append(StepLog(
                    argv=st.argv, rc=sr.rc, duration_s=sr.duration_s,
                    log_path=str(job_dir / f"step{idx}.log"), timed_out=sr.timed_out))
                rec.cpu_seconds += sr.duration_s
                if rec.license_features:
                    rec.license_seconds += sr.duration_s
                if sr.timed_out:
                    break
                if sr.rc != 0 and not st.allow_fail:
                    break
        finally:
            self.licenses.release(rec.license_features)

        result = adapter.parse(plan, results)
        rec.end_ts = time.time()
        rec.artifacts = dict(result.artifacts)
        rec.summary = result.summary
        rec.status = _job_status(result, results)
        rec.result = {"ok": result.ok, "status": result.status,
                      "metrics": result.metrics, "summary": result.summary,
                      "diagnostics": [d.__dict__ for d in result.diagnostics]}
        self._persist(rec)
        if key and reusable(rec, job_dir, plan):
            self.cache.remember(key, rec.id, rec.artifacts)
        return rec, result

    # ---- detached jobs (FR-JOB-04) -----------------------------------------

    def submit_async(self, plan: Plan, adapter: Adapter, **kw) -> JobRecord:
        """Start a job and return its ``running`` record immediately.

        The placeholder is persisted BEFORE the thread starts, so a crash
        between launch and completion leaves a job that can be found and
        reported rather than one that silently never existed.
        """
        job_id = self._next_id()
        (self.store_dir / job_id).mkdir(parents=True, exist_ok=True)
        rec = JobRecord(id=job_id, kind=plan.kind, adapter=adapter.name,
                        tool_version=adapter.version(), status="running",
                        submit_ts=time.time(), start_ts=time.time(),
                        input_files=list(kw.get("input_files") or []),
                        seed=kw.get("seed"),
                        license_features=list(plan.license_features))
        rec.compute_inputs_hash()
        rec.detached = True
        self._persist(rec)

        def _work():
            try:
                # the real run mints its own id; the placeholder records where
                # the answer landed, so either id leads a reader to the result
                done, _res = self.submit(plan, adapter, **kw)
                rec.status, rec.summary = done.status, done.summary
                rec.artifacts, rec.result = dict(done.artifacts), dict(done.result)
                rec.steps, rec.cpu_seconds = list(done.steps), done.cpu_seconds
                rec.license_seconds = done.license_seconds
                rec.ran_as = done.id
            except Exception as e:                  # a crash is a job outcome
                rec.status = "error"
                rec.summary = f"{type(e).__name__}: {e}"
            finally:
                rec.end_ts = time.time()
                self._persist(rec)
                with self._lock:
                    self._async.pop(rec.id, None)

        th = threading.Thread(target=_work, daemon=True,
                              name=f"chipchamp-{job_id}")
        with self._lock:
            self._async[job_id] = th
        th.start()
        return rec

    def poll(self, job_id: str) -> Optional[JobRecord]:
        """Current record. Reads from disk, so it works across processes —
        a detached job outlives the session that launched it."""
        return self.get(job_id)

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Optional[JobRecord]:
        """Block until a detached job leaves ``running``. None on timeout."""
        with self._lock:
            th = self._async.get(job_id)
        if th is not None:
            th.join(timeout=timeout)
            # still alive → the wait expired. Returning the record here would
            # hand back a `running` job as though it were an answer.
            return None if th.is_alive() else self.get(job_id)
        # launched by another process: poll the store instead of the thread
        deadline = time.time() + (timeout if timeout is not None else 1e9)
        while time.time() < deadline:
            rec = self.get(job_id)
            if rec is None or rec.status != "running":
                return rec
            time.sleep(1.0)
        return None

    def pending(self) -> list[JobRecord]:
        """Every job the store still believes is running."""
        return [r for r in self.list_jobs() if r.status == "running"]

    def _run_step(self, st, job_dir: Path, idx: int, timeout,
                  env_modules: list[str] | None = None) -> StepResult:
        env = {k: v for k, v in os.environ.items() if k in self.env_allowlist or True}
        env.update(st.env)
        argv = wrap_with_modules(st.argv, env_modules or [])
        from .farm import plan_submission
        argv, _farm_note = plan_submission(argv, self.farm)
        t0 = time.time()
        timed_out = False
        # Live log: both streams interleave into <job>/live.log AS THEY ARRIVE,
        # so a UI can tail a running build/sim instead of showing a dead
        # spinner for minutes. The per-step separated log (step<idx>.log) is
        # written unchanged at step end — adapters and grep_log see the exact
        # same artifacts as before; live.log is a purely additive view.
        live_path = job_dir / "live.log"
        observer = getattr(self, "on_step", None)
        if observer is not None:
            try:
                observer(job_dir.name, str(live_path))
            except Exception:
                pass  # a UI bug must never take down a job
        out, err = "", ""
        try:
            with open(live_path, "a", errors="replace") as live:
                live.write(f"$ {' '.join(st.argv)}\n")
                live.flush()
                lock = threading.Lock()
                bufs = {"out": [], "err": []}

                def pump(stream, key):
                    for line in iter(stream.readline, ""):
                        bufs[key].append(line)
                        try:
                            with lock:
                                live.write(line)
                                live.flush()
                        except ValueError:
                            pass  # live view closed; keep draining the record
                    stream.close()

                p = subprocess.Popen(argv, cwd=st.cwd, env=env, text=True,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     errors="replace")
                pumps = [threading.Thread(target=pump, args=(p.stdout, "out"),
                                          daemon=True),
                         threading.Thread(target=pump, args=(p.stderr, "err"),
                                          daemon=True)]
                for t in pumps:
                    t.start()
                try:
                    rc = p.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                    rc, timed_out = 124, True
                for t in pumps:
                    t.join(timeout=5)
                out, err = "".join(bufs["out"]), "".join(bufs["err"])
                if timed_out:
                    err += "\n[timed out]"
        except (OSError, subprocess.SubprocessError) as e:
            rc, out, err = 127, "", f"{type(e).__name__}: {e}"
        dur = time.time() - t0
        # persist the raw log (never streamed into model context — SPEC §8.5 FR-JOB-07)
        log = f"$ {' '.join(st.argv)}\n[cwd={st.cwd}] [rc={rc}] [{dur:.2f}s]\n"
        log += "--- stdout ---\n" + (out or "") + "\n--- stderr ---\n" + (err or "")
        atomic_write(job_dir / f"step{idx}.log", log)
        return StepResult(argv=st.argv, rc=rc, stdout=out or "", stderr=err or "",
                          duration_s=dur, timed_out=timed_out)

    def _persist(self, rec: JobRecord) -> None:
        atomic_write(self.store_dir / rec.id / "record.json", dump_json(rec.to_dict()))

    # ---- store queries ------------------------------------------------------

    def get(self, job_id: str) -> Optional[JobRecord]:
        f = self.store_dir / job_id / "record.json"
        if not f.exists():
            return None
        return JobRecord.from_dict(load_json(f))

    def list_jobs(self) -> list[JobRecord]:
        out = []
        for d in sorted(self.store_dir.glob("J-*")):
            rec = self.get(d.name)
            if rec:
                out.append(rec)
        return out

    def repro_command(self, job_id: str) -> Optional[str]:
        rec = self.get(job_id)
        if not rec:
            return None
        lines = [f"# repro {job_id} ({rec.kind} via {rec.adapter} {rec.tool_version})",
                 f"# inputs_hash={rec.inputs_hash} seed={rec.seed}"]
        for m in rec.env_modules:
            lines.append(f"module load {m}")
        for st in rec.steps:
            lines.append(" ".join(_quote(a) for a in st.argv))
        return "\n".join(lines)

    def grep_log(self, job_id: str, pattern: str, window: int = 3,
                 max_hits: int = 40) -> list[str]:
        """Windowed grep over a job's logs (SPEC §8.5 FR-JOB-07: logs are a query
        surface; full logs never enter model context)."""
        import re
        rx = re.compile(pattern)
        hits: list[str] = []
        for st in self.get(job_id).steps if self.get(job_id) else []:
            p = Path(st.log_path)
            if not p.exists():
                continue
            lines = p.read_text(errors="replace").splitlines()
            for i, ln in enumerate(lines):
                if rx.search(ln):
                    lo, hi = max(0, i - window), min(len(lines), i + window + 1)
                    hits.append("\n".join(lines[lo:hi]))
                    if len(hits) >= max_hits:
                        return hits
        return hits


def wrap_with_modules(argv: list[str], modules: list[str]) -> list[str]:
    """FR-PROJ-03: EDA farms manage tool versions with environment modules
    (`module load synopsys/vcs/2024.09`). When a job declares modules and the
    host has the modules system, wrap the command in a login shell that loads
    them first; otherwise run unwrapped (modules stay recorded for repro)."""
    import shlex
    import shutil
    if not modules:
        return argv
    has_modules = bool(os.environ.get("MODULESHOME")) or \
        shutil.which("modulecmd") is not None
    if not has_modules:
        return argv
    loads = " && ".join(f"module load {shlex.quote(m)}" for m in modules)
    return ["bash", "-lc", f"{loads} && exec {shlex.join(argv)}"]


def _job_status(result: ToolResult, steps: list[StepResult]) -> str:
    if any(s.timed_out for s in steps):
        return "timeout"
    if result.kind == "sim":
        return {"pass": "passed", "fail": "failed", "timeout": "timeout"}.get(
            result.status, "error" if result.status == "error" else "failed")
    if result.kind == "lec":
        return "passed" if result.status == "equivalent" else "failed"
    if result.kind == "formal":
        return "passed" if result.status == "pass" else "failed"
    return "passed" if result.ok else "failed"


def _quote(s: str) -> str:
    return f'"{s}"' if " " in s else s
