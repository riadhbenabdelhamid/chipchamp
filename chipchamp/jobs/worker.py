"""The detached-job worker: a job that outlives the session that launched it.

`sim.run(detach=true)` promised (FR-JOB-04) that the caller could get on
with other work — or exit — and collect the result later with job.wait or
`chipchamp wake`. It ran the job on a daemon thread, which dies with the
interpreter; a headless run that launched a detached sim and returned left
the record `running` forever and `wake` parked on it every time.

Now `submit_async` serialises the plan to <job>/launch.json and starts
``python -m chipchamp.jobs.worker <store> <job_id>`` in its own session.
The worker rebuilds the plan and adapter, runs the job through the same
JobRunner.submit() everyone else uses, records its pid and a heartbeat on
the placeholder record, and writes the outcome. Readers (job.status,
job.wait, wake) detect a worker that died mid-job from the dead pid.

Adapters are rebuilt by name from the registry, or — for an adapter class
that is importable but not registered — by ``module:QualName``. A class
that is neither (one defined inside a test function) runs on the old
in-process thread; the launching process then marks it abandoned at exit.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

HEARTBEAT_S = 10.0


# ---- plan / adapter (de)serialisation ---------------------------------------

def plan_to_dict(plan) -> dict:
    return {"kind": plan.kind, "adapter": plan.adapter, "workdir": plan.workdir,
            "steps": [{"argv": list(s.argv), "cwd": s.cwd, "env": dict(s.env),
                       "allow_fail": bool(s.allow_fail)} for s in plan.steps],
            "artifacts": dict(plan.artifacts),
            "license_features": list(plan.license_features),
            "meta": dict(plan.meta)}


def plan_from_dict(d: dict):
    from ..adapters.base import Plan, Step
    return Plan(kind=d["kind"], adapter=d["adapter"], workdir=d["workdir"],
                steps=[Step(argv=list(s["argv"]), cwd=s["cwd"],
                            env=dict(s.get("env") or {}),
                            allow_fail=bool(s.get("allow_fail")))
                       for s in d.get("steps", [])],
                artifacts=dict(d.get("artifacts") or {}),
                license_features=list(d.get("license_features") or []),
                meta=dict(d.get("meta") or {}))


def adapter_ref(adapter) -> str | None:
    """'module:QualName' when a fresh process could import this exact class;
    None when it could not (a class defined inside a function, or a module
    that is not importable by name)."""
    cls = type(adapter)
    if "<locals>" in cls.__qualname__ or cls.__module__ in ("__main__", "builtins"):
        return None
    try:
        mod = importlib.import_module(cls.__module__)
        obj = mod
        for part in cls.__qualname__.split("."):
            obj = getattr(obj, part)
        return f"{cls.__module__}:{cls.__qualname__}" if obj is cls else None
    except Exception:
        return None


def load_adapter(ref: str | None, name: str):
    from ..adapters.registry import AdapterRegistry
    if ref:
        mod_name, _, qual = ref.partition(":")
        obj = importlib.import_module(mod_name)
        for part in qual.split("."):
            obj = getattr(obj, part)
        return obj()
    return AdapterRegistry().get(name)


# ---- the worker process ------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m chipchamp.jobs.worker <store_dir> <job_id>",
              file=sys.stderr)
        return 2
    store, job_id = Path(argv[0]), argv[1]
    from .runner import JobRunner
    launch = json.loads((store / job_id / "launch.json").read_text())
    runner = JobRunner(str(store), None, **(launch.get("runner_kwargs") or {}))
    rec = runner.get(job_id)
    if rec is None:
        return 3
    rec.pid = os.getpid()
    rec.heartbeat_ts = time.time()
    runner._persist(rec)

    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_S):
            try:
                rec.heartbeat_ts = time.time()
                runner._persist(rec)
            except OSError:
                pass
    threading.Thread(target=beat, daemon=True, name="heartbeat").start()

    try:
        plan = plan_from_dict(launch["plan"])
        adapter = load_adapter(launch.get("adapter_ref"), launch["adapter_name"])
        done, _res = runner.submit(plan, adapter, **(launch.get("kwargs") or {}))
        runner._adopt_outcome(rec, done)
    except BaseException as e:                       # a crash is a job outcome
        rec.status = "error"
        rec.summary = f"{type(e).__name__}: {e}"
    finally:
        stop.set()
        rec.end_ts = time.time()
        runner._persist(rec)
    return 0 if rec.status in ("passed", "failed") else 1


if __name__ == "__main__":
    sys.exit(main())
