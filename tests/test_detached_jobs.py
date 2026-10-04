"""A detached job must outlive the process that launched it (FR-JOB-04), and
a reader must be able to tell a running job from one whose worker died."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from chipchamp.adapters.base import Plan, Step
from chipchamp.jobs.record import JobRecord
from chipchamp.jobs.runner import JobRunner

HERE = Path(__file__).parent


def _plan(tmp_path, secs="0.3"):
    return Plan(kind="sim", adapter="slowok", workdir=str(tmp_path),
                steps=[Step(argv=["sleep", secs], cwd=str(tmp_path))])


def test_an_importable_adapter_runs_in_a_worker_process(tmp_path):
    from _detached_fake import SlowOk
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    rec = r.submit_async(_plan(tmp_path), SlowOk(), input_files=[])
    assert rec.status == "running" and rec.pid and rec.pid != os.getpid()
    assert (tmp_path / "runs" / rec.id / "launch.json").exists()
    done = r.wait(rec.id, timeout=60)
    assert done.status == "passed" and done.summary == "worker slept and passed"
    assert done.ran_as and done.ran_as != rec.id
    assert (tmp_path / "runs" / rec.id / "worker.log").exists()


def test_the_job_survives_the_launching_process(tmp_path):
    store = tmp_path / "runs"
    launcher = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(HERE)!r})\n"
        "from _detached_fake import SlowOk\n"
        "from chipchamp.adapters.base import Plan, Step\n"
        "from chipchamp.jobs.runner import JobRunner\n"
        f"r = JobRunner({str(store)!r}, registry=None)\n"
        f"p = Plan(kind='sim', adapter='slowok', workdir={str(tmp_path)!r}, "
        f"steps=[Step(argv=['sleep', '1.5'], cwd={str(tmp_path)!r})])\n"
        "rec = r.submit_async(p, SlowOk(), input_files=[])\n"
        "print(rec.id, os.getpid())\n"
    )
    out = subprocess.run([sys.executable, "-c", launcher], capture_output=True,
                         text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    job_id, launcher_pid = out.stdout.split()
    # the launcher is gone; the job is still being run by someone else
    reader = JobRunner(str(store), registry=None)
    rec = reader.get(job_id)
    assert rec.status == "running" and rec.pid != int(launcher_pid)
    done = reader.wait(job_id, timeout=60)
    assert done is not None and done.status == "passed", done and done.summary
    assert done.ran_as


def test_a_dead_worker_is_reported_not_parked_forever(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    (tmp_path / "runs" / "J-0007").mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    rec = JobRecord(id="J-0007", kind="sim", adapter="x", status="running",
                    detached=True, pid=dead.pid, heartbeat_ts=time.time() - 60)
    r._persist(rec)
    got = r.get("J-0007")
    assert got.status == "error" and "died" in got.summary
    assert r.get("J-0007").status == "error"           # persisted
    assert r.pending() == []                           # wake no longer parks on it


def test_an_in_process_job_is_marked_abandoned_at_exit(tmp_path):
    from chipchamp.adapters.base import Adapter, ToolResult

    class Local(Adapter):                 # defined in a function: not importable
        name = "local"

        def version(self):
            return "1"

        def available(self):
            return True

        def plan(self, **kw):
            raise NotImplementedError

        def parse(self, plan, results):
            return ToolResult(ok=True, kind="sim", adapter=self.name, status="pass")

    r = JobRunner(str(tmp_path / "runs"), registry=None)
    plan = Plan(kind="sim", adapter="local", workdir=str(tmp_path),
                steps=[Step(argv=["sleep", "2"], cwd=str(tmp_path))])
    rec = r.submit_async(plan, Local(), input_files=[])
    assert rec.pid == os.getpid()                      # the thread fallback
    r._mark_abandoned()                                # what atexit will do
    got = r.get(rec.id)
    assert got.status == "error" and "abandoned" in got.summary
