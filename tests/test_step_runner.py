"""A step is a process TREE and its output can be huge: a timeout must kill
the grandchildren too, and a log must not be held in memory to be parsed
or grepped."""
from __future__ import annotations

import os
import sys
import time

from chipchamp.adapters.base import Step
from chipchamp.jobs.record import JobRecord, StepLog
from chipchamp.jobs.runner import STEP_TEXT_CAP, JobRunner, _BoundedText


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                      # a zombie still answers kill(0); reap check
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except OSError:
        return False


def test_timeout_kills_the_grandchild_too(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    job = tmp_path / "runs" / "J-T"
    job.mkdir(parents=True)
    pidfile = tmp_path / "pid"
    st = Step(argv=["bash", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
              cwd=str(tmp_path))
    sr = r._run_step(st, job, 0, timeout=0.5)
    assert sr.timed_out and sr.rc == 124
    pid = int(pidfile.read_text())
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid), "the grandchild sleep outlived the timeout"


def test_bounded_text_keeps_head_and_tail():
    b = _BoundedText(cap=400)
    for i in range(100):
        b.append(f"line{i:03d}xxxxxxxxx\n")        # 17 bytes each
    t = b.text()
    assert t.startswith("line000") and t.rstrip().endswith("line099xxxxxxxxx")
    assert "omitted from memory" in t and b.dropped > 0
    assert len(t) < 400 + 120


def test_huge_output_is_complete_on_disk_but_bounded_in_memory(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    job = tmp_path / "runs" / "J-B"
    job.mkdir(parents=True)
    n = 6 * 1024                                   # ~6 MB of 1 KB lines
    st = Step(argv=[sys.executable, "-c",
                    f"import sys\nfor i in range({n}): sys.stdout.write('L%05d' % i + 'x'*1017 + '\\n')\n"
                    "sys.stderr.write('tail-err\\n')"],
              cwd=str(tmp_path))
    sr = r._run_step(st, job, 0, timeout=120)
    assert sr.rc == 0
    assert len(sr.stdout) <= STEP_TEXT_CAP + 200
    assert sr.stdout.startswith("L00000") and f"L{n-1:05d}" in sr.stdout
    assert "omitted from memory" in sr.stdout and "tail-err" in sr.stderr
    log = job / "step0.log"
    assert log.stat().st_size > n * 1024          # the whole stream, on disk
    assert not (job / "step0.out").exists() and not (job / "step0.err").exists()
    head = log.read_text(errors="replace")[:4096]
    assert "--- stdout ---" in head and "L00000" in head


def test_grep_log_streams_and_keeps_the_window(tmp_path):
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    (tmp_path / "runs" / "J-G").mkdir(parents=True)
    log = tmp_path / "runs" / "J-G" / "step0.log"
    log.write_text("\n".join(f"l{i}" if i not in (5, 50) else f"ERROR at {i}"
                             for i in range(60)) + "\n")
    rec = JobRecord(id="J-G", kind="sim", adapter="x", status="failed",
                    steps=[StepLog(argv=[], rc=1, duration_s=0, log_path=str(log))])
    r._persist(rec)
    hits = r.grep_log("J-G", "ERROR", window=2)
    assert hits == ["l3\nl4\nERROR at 5\nl6\nl7", "l48\nl49\nERROR at 50\nl51\nl52"]
    assert r.grep_log("J-G", "l59", window=2) == ["l57\nl58\nl59"]   # end of file
    assert len(r.grep_log("J-G", "l", window=0, max_hits=7)) == 7
