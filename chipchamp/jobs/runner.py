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

import atexit
import collections
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from ..adapters.base import Adapter, Plan, StepResult, ToolResult
from ..util.hashing import hash_manifest
from ..util.ids import new_job_id
from ..util.jsonio import atomic_write, dump_json, load_json
from .record import JobRecord, StepLog
from .env import scrub_env

_JOB_DIR_RX = re.compile(r"^J-(\d+)$")

# Bytes of a step's stdout/stderr kept in MEMORY (head + tail). The full
# stream always goes to the step log on disk; this is what adapters parse
# and what rides in StepResult. A simulation with $display in a loop put
# gigabytes into a Python list before.
STEP_TEXT_CAP = 4 * 1024 * 1024


class _BoundedText:
    """Head and tail of a stream within `cap` bytes; the middle is dropped
    with a marker saying how much, and where the whole thing lives."""

    def __init__(self, cap: int = STEP_TEXT_CAP):
        self.head_cap = cap // 4
        self.tail_cap = cap - self.head_cap
        self.head: list[str] = []
        self.head_n = 0
        self.tail: collections.deque = collections.deque()
        self.tail_n = 0
        self.dropped = 0

    def append(self, line: str) -> None:
        n = len(line)
        if not self.tail and self.head_n + n <= self.head_cap:
            self.head.append(line)
            self.head_n += n
            return
        self.tail.append(line)
        self.tail_n += n
        while self.tail_n > self.tail_cap and self.tail:
            d = self.tail.popleft()
            self.tail_n -= len(d)
            self.dropped += len(d)

    def text(self) -> str:
        if not self.dropped:
            return "".join(self.head) + "".join(self.tail)
        return ("".join(self.head)
                + f"\n[... {self.dropped} bytes omitted from memory — the full "
                  f"stream is in the step log ...]\n"
                + "".join(self.tail))


def _pid_alive(pid: int) -> bool:
    """Is `pid` a live (non-zombie) process? Permission errors count as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


def _builtin_adapter_names() -> set:
    from ..adapters.registry import _BUILTIN
    return set(_BUILTIN)


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill the step and everything it spawned. Steps run in their own
    session, so the group id is the step's pid; a plain p.kill() only took
    the wrapper (bash -lc, a vendor launcher) and left the real tool
    running, holding CPU, licenses and the pipe."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        try:
            p.kill()
        except OSError:
            pass


class LicensePool:
    """Thread semaphores plus process-shared seats for a job store.

    OS file locks are released if a worker dies, so a crash cannot leave a
    permanent seat reservation. All runners using the store share the seats.
    """

    def __init__(self, pools: dict[str, int] | None = None, shared_dir=None):
        self._sem = {feat: threading.BoundedSemaphore(max(1, n))
                     for feat, n in (pools or {}).items()}
        self._counts = dict(pools or {})
        self.shared_dir = Path(shared_dir) if shared_dir else None
        if self.shared_dir is not None:
            self.shared_dir.mkdir(parents=True, exist_ok=True)
        self._held: dict = {}
        self._held_lock = threading.Lock()

    def _file_seat(self, feature, deadline):
        import fcntl
        from ..util.hashing import sha256_text
        while True:
            for i in range(max(1, self._counts[feature])):
                path = self.shared_dir / f"{sha256_text(feature)}-{i}.lock"
                fh = open(path, "a+")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fh
                except BlockingIOError:
                    fh.close()
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(min(0.05, max(0, deadline - time.monotonic()))
                       if deadline is not None else 0.05)

    def acquire(self, features: list[str], timeout: float | None = None) -> bool:
        """Take one seat of every pooled feature, or none of them.

        Features are taken in sorted order: two jobs asking for [A, B] and
        [B, A] with no timeout each held one and waited for the other
        forever. Features with no pool are unlimited and pass through."""
        acquired = []
        deadline = time.monotonic() + timeout if timeout is not None else None
        for f in sorted(set(features)):
            if f in self._sem:
                remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
                if not self._sem[f].acquire(timeout=remaining):
                    self.release(acquired)
                    return False
                if self.shared_dir is not None:
                    try:
                        fh = self._file_seat(f, deadline)
                    except BaseException:
                        self._sem[f].release()
                        self.release(acquired)
                        raise
                    if fh is None:
                        self._sem[f].release()
                        self.release(acquired)
                        return False
                    with self._held_lock:
                        self._held.setdefault((threading.get_ident(), f), []).append(fh)
                acquired.append(f)
        return True

    def release(self, features: list[str]) -> None:
        for f in sorted(set(features)):
            if f in self._sem:
                if self.shared_dir is not None:
                    with self._held_lock:
                        held = self._held.get((threading.get_ident(), f), [])
                        fh = held.pop() if held else None
                    if fh is None:
                        continue
                    fh.close()
                try:
                    self._sem[f].release()
                except ValueError:
                    pass   # released more than held: should be unreachable now


class JobRunner:
    def __init__(self, store_dir: str, registry, *, env_modules: dict | None = None,
                 licenses: dict | None = None, env_allowlist: list[str] | None = None,
                 env_deny: list[str] | None = None, farm=None, cache: bool = True,
                 license_wait_s: float | None = None):
        from .cache import JobCache
        from .farm import FarmConfig
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.env_modules = env_modules or {}
        self.licenses = LicensePool({f: v.get("tokens", 1) if isinstance(v, dict) else v
                                     for f, v in (licenses or {}).items()},
                                    shared_dir=self.store_dir / ".licenses")
        # None/empty = pass the environment through minus secrets (jobs.env);
        # a list = strict allowlist for hermetic steps. Never the old
        # ["PATH","HOME","USER"] default: honoured, it broke every EDA flow.
        self.env_allowlist = list(env_allowlist or [])
        self.env_deny = list(env_deny or [])
        # How long a job may queue for a license seat — distinct from the run
        # timeout, which used to double as the queue wait. None = wait.
        self.license_wait_s = license_wait_s
        self.farm = FarmConfig(**farm) if isinstance(farm, dict) else farm or FarmConfig()
        self.cache = JobCache(self.store_dir, enabled=cache)
        self._seq = self._load_seq()
        self._lock = threading.Lock()
        self._async: dict[str, object] = {}     # job id -> Thread | Popen
        self._atexit_armed = False

    def _load_seq(self) -> int:
        f = self.store_dir / "seq.txt"
        if f.exists():
            try:
                return int(f.read_text().strip())
            except ValueError:
                return 0
        return 0

    def _highest_on_disk(self) -> int:
        try:
            names = os.listdir(self.store_dir)
        except OSError:
            return 0
        best = 0
        for n in names:
            m = _JOB_DIR_RX.match(n)
            if m:
                best = max(best, int(m.group(1)))
        return best

    def _next_id(self) -> str:
        """Allocate the next job id by CLAIMING its directory.

        seq.txt was read once per process and written non-atomically, so the
        REPL and a cron `wake` (or two engineers on a shared [jobs] store)
        could both hand out J-0042 and the second silently overwrote the
        first's record and logs; an empty seq.txt after a crash restarted
        numbering at J-0001 over jobs that evidence bundles still cite.
        Now the candidate is one past the highest of (this process's
        counter, seq.txt, the highest J-* directory on disk), and an
        exclusive mkdir is the claim — a collision just moves on to the next
        number. seq.txt stays as a hint for readers and is written atomically.
        """
        with self._lock:
            # the store dir can vanish mid-process (an experiment harness
            # sequestering the archive, a cleanup script) — id allocation is
            # the first write of every submit, so it must not be the thing
            # that turns a missing directory into a failed job
            self.store_dir.mkdir(parents=True, exist_ok=True)
            seq = max(self._seq, self._load_seq(), self._highest_on_disk())
            for _ in range(100_000):
                seq += 1
                job_id = new_job_id(seq)
                try:
                    os.mkdir(self.store_dir / job_id)
                except FileExistsError:
                    continue
                self._seq = seq
                try:
                    atomic_write(self.store_dir / "seq.txt", str(seq))
                except OSError:
                    pass   # the directory claim is the truth; the hint is best-effort
                return job_id
            raise RuntimeError("could not allocate a job id: store is full of claims")

    def submit(self, plan: Plan, adapter: Adapter, *,
               input_files: Optional[list[str]] = None,
               defines: Optional[dict] = None, seed: Optional[int] = None,
               timeout: Optional[float] = None,
               env_modules: Optional[list[str]] = None,
               no_cache: bool = False,
               idle_timeout: Optional[float] = None) -> tuple[JobRecord, ToolResult]:
        from .cache import cache_key, result_from_record, reusable
        from .inputs import capture_inputs, finish_capture
        snapshot = capture_inputs(plan, input_files or [])
        key = ""
        ih = ""
        try:
            ih = hash_manifest(input_files or [])
            key = cache_key(plan, adapter, inputs_hash=ih, seed=seed,
                            defines=defines, env_modules=env_modules,
                            snapshot=snapshot,
                            environment=scrub_env(allowlist=self.env_allowlist, deny=self.env_deny))
        except Exception:
            key = ""   # an unhashable plan just means no reuse, never a fail
        if self.cache.enabled and not no_cache:
            hit = self.cache.lookup(key) if key else None
            if hit:
                rec = self.get(hit)
                if rec is not None and reusable(rec, self.store_dir / hit, plan,
                                                inputs_hash=ih, adapter=adapter.name):
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
        rec.input_snapshot = snapshot
        rec.execution_key = key
        for attr in ("verification_hash", "test_name", "target_name", "test_config_hash", "regression_id"):
            setattr(rec, attr, plan.meta.get(attr, ""))
        rec.compute_inputs_hash()
        rec.start_ts = time.time()
        self._persist(rec)  # Preserve a launched regression attempt across interruption.

        if not self.licenses.acquire(rec.license_features,
                                     timeout=self.license_wait_s):
            # A job used to wait, then RUN ANYWAY without a seat — and its
            # release in `finally` handed the pool a seat it never took, so
            # concurrent jobs oversubscribed the real licenses from then on.
            rec.end_ts = time.time()
            rec.status = "error"
            rec.summary = (f"no license seat for {sorted(rec.license_features)} "
                           f"within {self.license_wait_s}s — not run")
            rec.result = {"ok": False, "status": "error", "metrics": {},
                          "summary": rec.summary, "diagnostics": []}
            self._persist(rec)
            return rec, ToolResult(ok=False, kind=plan.kind, adapter=adapter.name,
                                   status="error", summary=rec.summary)
        results: list[StepResult] = []
        try:
            for idx, st in enumerate(plan.steps):
                sr = self._run_step(st, job_dir, idx, timeout,
                                    env_modules=rec.env_modules,
                                    idle_timeout=idle_timeout)
                results.append(sr)
                rec.steps.append(StepLog(
                    argv=st.argv, rc=sr.rc, duration_s=sr.duration_s,
                    log_path=str(job_dir / f"step{idx}.log"), timed_out=sr.timed_out,
                    timeout_reason=sr.timeout_reason))
                rec.cpu_seconds += sr.duration_s
                if rec.license_features:
                    rec.license_seconds += sr.duration_s
                if sr.timed_out:
                    break
                if sr.rc != 0 and not st.allow_fail:
                    break
                if idx == plan.meta.get("capture_after_step"):
                    # Generated headers now exist, and the native compilation
                    # has not started. Capture its complete preprocessor closure.
                    captured = capture_inputs(plan, input_files or [])
                    if any(captured["files"].get(p) != digest for p, digest in snapshot["files"].items()):
                        captured["unstable"] = True
                        captured["complete"] = False
                    snapshot = captured
                    key = cache_key(plan, adapter, inputs_hash=ih, seed=seed,
                        defines=defines, env_modules=env_modules, snapshot=snapshot,
                        environment=scrub_env(allowlist=self.env_allowlist, deny=self.env_deny))
                    rec.execution_key = key
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
        rec.input_snapshot = finish_capture(plan, snapshot, input_files or [])
        if (plan.meta.get("python_trace") or plan.meta.get("cpp_dependencies")) and (self.env_allowlist or self.env_deny or rec.env_modules):
            rec.input_snapshot["complete"] = False
            rec.input_snapshot["reasons"].append("dependency probing with custom environment policy/modules is unsupported")
        if not rec.input_snapshot["complete"]:
            key = rec.execution_key = ""
        elif plan.meta.get("python_trace"):
            key = rec.execution_key = cache_key(plan, adapter, inputs_hash=ih, seed=seed,
                defines=defines, env_modules=env_modules, snapshot=rec.input_snapshot,
                environment=scrub_env(allowlist=self.env_allowlist, deny=self.env_deny))
        from ..util.hashing import sha256_file
        for path in [*(s.log_path for s in rec.steps), *rec.artifacts.values()]:
            if path:
                try:
                    rec.evidence_hashes[path] = sha256_file(path)
                except OSError:
                    rec.evidence_hashes[path] = "MISSING"
        self._persist(rec)
        if key and reusable(rec, job_dir, plan):
            self.cache.remember(key, rec.id, rec.artifacts)
        return rec, result

    # ---- detached jobs (FR-JOB-04) -----------------------------------------

    @staticmethod
    def _adopt_outcome(placeholder: JobRecord, done: JobRecord) -> None:
        """Copy a finished run's outcome onto the detached placeholder. The
        real run mints its own id; `ran_as` records where the answer landed,
        so either id leads a reader to the result."""
        placeholder.status, placeholder.summary = done.status, done.summary
        placeholder.artifacts = dict(done.artifacts)
        placeholder.result = dict(done.result or {})
        placeholder.steps, placeholder.cpu_seconds = list(done.steps), done.cpu_seconds
        placeholder.license_seconds = done.license_seconds
        placeholder.ran_as = done.id
        for attr in ("input_snapshot", "inputs_hash", "input_files", "verification_hash",
                     "test_name", "target_name", "test_config_hash", "end_ts",
                     "execution_key", "evidence_hashes", "regression_id"):
            setattr(placeholder, attr, getattr(done, attr))

    def submit_async(self, plan: Plan, adapter: Adapter, **kw) -> JobRecord:
        """Start a job and return its ``running`` record immediately.

        The placeholder is persisted BEFORE anything starts, so a crash
        between launch and completion leaves a job that can be found and
        reported rather than one that silently never existed.

        The job runs in a separate worker PROCESS (jobs/worker.py) in its own
        session, so it survives the launching process — the detach contract.
        An adapter a fresh process cannot import (a class defined inside a
        function) falls back to an in-process thread; those are marked
        abandoned if the launcher exits first.
        """
        from .worker import adapter_ref, plan_to_dict
        # Detached work always executes; its reservation and completion cost
        # must not be confused with a free synchronous cache hit.
        kw["no_cache"] = True
        job_id = self._next_id()
        job_dir = self.store_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        rec = JobRecord(id=job_id, kind=plan.kind, adapter=adapter.name,
                        tool_version=adapter.version(), status="running",
                        submit_ts=time.time(), start_ts=time.time(),
                        input_files=list(kw.get("input_files") or []),
                        seed=kw.get("seed"),
                        license_features=list(plan.license_features))
        rec.compute_inputs_hash()
        rec.detached = True
        self._persist(rec)

        ref = adapter_ref(adapter)
        importable = ref is not None or adapter.name in _builtin_adapter_names()
        if importable:
            from dataclasses import asdict
            launch = {"plan": plan_to_dict(plan), "kwargs": kw,
                      "adapter_name": adapter.name, "adapter_ref": ref,
                      "runner_kwargs": {"env_allowlist": self.env_allowlist,
                                        "env_deny": self.env_deny,
                                        "license_wait_s": self.license_wait_s,
                                        "env_modules": self.env_modules,
                                        "licenses": self.licenses._counts,
                                        "farm": asdict(self.farm),
                                        "cache": self.cache.enabled}}
            try:
                atomic_write(job_dir / "launch.json", dump_json(launch))
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join(
                    [p for p in sys.path if p] + [env.get("PYTHONPATH", "")]).strip(os.pathsep)
                log = open(job_dir / "worker.log", "a")
                p = subprocess.Popen(
                    [sys.executable, "-m", "chipchamp.jobs.worker",
                     str(self.store_dir), job_id],
                    cwd=plan.workdir or None, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                log.close()
            except (OSError, TypeError, ValueError) as e:
                # could not start a worker: run in-process rather than lose the job
                rec.summary = f"worker spawn failed ({type(e).__name__}: {e}); ran in-process"
                importable = False
            else:
                rec.pid = p.pid
                rec.heartbeat_ts = time.time()
                self._persist(rec)
                with self._lock:
                    self._async[job_id] = p
                return rec

        # ---- in-process fallback ----
        rec.pid = os.getpid()
        rec.heartbeat_ts = time.time()
        self._persist(rec)

        def _work():
            try:
                done, _res = self.submit(plan, adapter, **kw)
                self._adopt_outcome(rec, done)
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
            if not self._atexit_armed:
                atexit.register(self._mark_abandoned)
                self._atexit_armed = True
        th.start()
        return rec

    def _mark_abandoned(self) -> None:
        """At interpreter exit: in-process detached jobs die with us, so say so
        instead of leaving `running` records that wake would park on forever."""
        with self._lock:
            items = list(self._async.items())
        for job_id, h in items:
            if isinstance(h, threading.Thread) and h.is_alive():
                rec = self.get(job_id)
                if rec and rec.status == "running":
                    rec.status = "error"
                    rec.summary = "abandoned: the launching process exited (in-process job)"
                    rec.end_ts = time.time()
                    try:
                        self._persist(rec)
                    except OSError:
                        pass

    def poll(self, job_id: str) -> Optional[JobRecord]:
        """Current record. Reads from disk, so it works across processes —
        a detached job outlives the session that launched it."""
        return self.get(job_id)

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Optional[JobRecord]:
        """Block until a detached job leaves ``running``. None on timeout."""
        with self._lock:
            h = self._async.get(job_id)
        if isinstance(h, threading.Thread):
            h.join(timeout=timeout)
            # still alive → the wait expired. Returning the record here would
            # hand back a `running` job as though it were an answer.
            return None if h.is_alive() else self.get(job_id)
        if isinstance(h, subprocess.Popen):
            try:
                h.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None
            with self._lock:
                self._async.pop(job_id, None)
            return self.get(job_id)
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
                  env_modules: list[str] | None = None,
                  idle_timeout: float | None = None) -> StepResult:
        """`timeout` bounds the wall clock; `idle_timeout` bounds silence —
        a tool that has printed nothing for that long is wedged, while one
        still printing progress is merely slow. Either kills the whole tree
        and marks the step timed_out with the reason."""
        env = scrub_env(os.environ, allowlist=self.env_allowlist, deny=self.env_deny)
        env.update(st.env)
        argv = wrap_with_modules(st.argv, env_modules or [])
        from .farm import plan_submission
        argv, _farm_note = plan_submission(argv, self.farm)
        t0 = time.time()
        timed_out = False
        timeout_reason = ""
        last_output = [t0]
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
        # Each stream goes straight to its own file as it arrives; memory
        # keeps only a bounded head+tail (what adapters parse). The two
        # files are stitched into step<idx>.log at the end — same separated
        # format readers always saw — without ever holding a log in RAM.
        out_path, err_path = job_dir / f"step{idx}.out", job_dir / f"step{idx}.err"
        try:
            with open(live_path, "a", errors="replace") as live, \
                    open(out_path, "w", errors="replace") as out_fh, \
                    open(err_path, "w", errors="replace") as err_fh:
                live.write(f"$ {' '.join(st.argv)}\n")
                live.flush()
                lock = threading.Lock()
                bufs = {"out": _BoundedText(), "err": _BoundedText()}
                files = {"out": out_fh, "err": err_fh}

                def pump(stream, key):
                    for line in iter(stream.readline, ""):
                        last_output[0] = time.time()
                        bufs[key].append(line)
                        files[key].write(line)
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
                                     errors="replace", start_new_session=True)
                pumps = [threading.Thread(target=pump, args=(p.stdout, "out"),
                                          daemon=True),
                         threading.Thread(target=pump, args=(p.stderr, "err"),
                                          daemon=True)]
                for t in pumps:
                    t.start()
                try:
                    wall_deadline = t0 + timeout if timeout else None
                    while True:
                        try:
                            # A fixed polling interval let short jobs finish
                            # successfully after their deadline. Bound the wait
                            # by both active deadlines, including subsecond ones.
                            now = time.time()
                            remaining = [0.5]
                            if wall_deadline:
                                remaining.append(max(0, wall_deadline - now))
                            if idle_timeout:
                                remaining.append(max(0, last_output[0] + idle_timeout - now))
                            rc = p.wait(timeout=min(remaining))
                            break
                        except subprocess.TimeoutExpired:
                            now = time.time()
                            if wall_deadline and now >= wall_deadline:
                                timeout_reason = "wall"
                            elif idle_timeout and now - last_output[0] >= idle_timeout:
                                timeout_reason = "idle"
                            else:
                                continue
                            _kill_tree(p)
                            p.wait()
                            rc, timed_out = 124, True
                            break
                except KeyboardInterrupt:
                    # own session => the terminal's SIGINT no longer reaches
                    # the step; take the whole tree down ourselves
                    _kill_tree(p)
                    p.wait()
                    raise
                for t in pumps:
                    t.join(timeout=5)
                out, err = bufs["out"].text(), bufs["err"].text()
                if timed_out:
                    note = (f"\n[timed out: no output for {idle_timeout:g}s]"
                            if timeout_reason == "idle"
                            else f"\n[timed out after {timeout:g}s]")
                    err += note
                    err_fh.write(note)
        except (OSError, subprocess.SubprocessError) as e:
            rc, out, err = 127, "", f"{type(e).__name__}: {e}"
            try:
                err_path.write_text(err)
            except OSError:
                pass
        dur = time.time() - t0
        # persist the raw log (never streamed into model context — SPEC §8.5 FR-JOB-07)
        log_path = job_dir / f"step{idx}.log"
        tmp = job_dir / f".step{idx}.log.tmp"
        try:
            with open(tmp, "w", errors="replace") as fh:
                fh.write(f"$ {' '.join(st.argv)}\n[cwd={st.cwd}] [rc={rc}] [{dur:.2f}s]\n")
                fh.write("--- stdout ---\n")
                if out_path.exists():
                    with open(out_path, errors="replace") as src:
                        shutil.copyfileobj(src, fh)
                fh.write("\n--- stderr ---\n")
                if err_path.exists():
                    with open(err_path, errors="replace") as src:
                        shutil.copyfileobj(src, fh)
            os.replace(tmp, log_path)
        finally:
            for f in (out_path, err_path, tmp):
                try:
                    f.unlink()
                except OSError:
                    pass
        return StepResult(argv=st.argv, rc=rc, stdout=out or "", stderr=err or "",
                          duration_s=dur, timed_out=timed_out,
                          timeout_reason=timeout_reason)

    def _persist(self, rec: JobRecord) -> None:
        atomic_write(self.store_dir / rec.id / "record.json", dump_json(rec.to_dict()))

    # ---- store queries ------------------------------------------------------

    def get(self, job_id: str) -> Optional[JobRecord]:
        f = self.store_dir / job_id / "record.json"
        if not f.exists():
            return None
        rec = JobRecord.from_dict(load_json(f))
        if rec.status == "running" and rec.detached and rec.pid:
            # a worker we own: reap it so a finished-but-unreaped pid is not
            # mistaken for a live one
            with self._lock:
                h = self._async.get(job_id)
            if isinstance(h, subprocess.Popen):
                h.poll()
            if not _pid_alive(rec.pid):
                rec.status = "error"
                rec.summary = (f"worker process {rec.pid} died before the job "
                               f"finished (lost)")
                rec.end_ts = time.time()
                try:
                    self._persist(rec)
                except OSError:
                    pass
        return rec

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
        rec = self.get(job_id)
        for st in rec.steps if rec else []:
            p = Path(st.log_path)
            if not p.exists():
                continue
            # streamed: a multi-GB sim log must not be read into memory to
            # answer a grep. `prev` is the window before a hit; an open hit
            # keeps collecting until `window` lines after it have passed.
            prev: collections.deque = collections.deque(maxlen=window)
            open_hits: list[list] = []     # [lines, remaining_after]
            with open(p, errors="replace") as fh:
                for raw in fh:
                    ln = raw.rstrip("\n")
                    for h in open_hits:
                        h[0].append(ln)
                        h[1] -= 1
                    for h in [h for h in open_hits if h[1] <= 0]:
                        open_hits.remove(h)
                        hits.append("\n".join(h[0]))
                        if len(hits) >= max_hits:
                            return hits
                    if rx.search(ln):
                        open_hits.append([list(prev) + [ln], window])
                    prev.append(ln)
            for h in open_hits:            # hits near the end of the file
                hits.append("\n".join(h[0]))
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
