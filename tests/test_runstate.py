"""The loop's run state, testable on its own: the ledger of what a run wrote,
and the per-scope verification tracker that decides green, redo and gap."""
from __future__ import annotations

from chipchamp.agent.runstate import (LEDGER_TAG, RunState, VerificationTracker,
                                      WorkspaceLedger)


def test_ledger_tracks_writes_deletes_and_epoch():
    led = WorkspaceLedger()
    assert not led.dirty and led.epoch == 0
    for op in (lambda: led.wrote("work/rtl/a.sv"), lambda: led.wrote("work/rtl/b.sv"),
               lambda: led.removed("work/rtl/a.sv")):
        op()
        led.bump()
    assert led.authored == {"work/rtl/b.sv"} and led.deleted == {"work/rtl/a.sv"}
    assert led.epoch == 3 and led.dirty
    led.wrote("work/rtl/a.sv")                      # re-created: no longer "deleted"
    assert led.deleted == set()


def test_ledger_turn_is_exactly_one_and_current():
    led = WorkspaceLedger()
    led.wrote("x.sv")
    t = [{"role": "user", "content": "task"},
         {"role": "user", "guard": "ledger", "content": LEDGER_TAG + " stale"}]
    led.refresh(t, "lint(x.sv) J-0001 passed")
    ledgers = [m for m in t if str(m.get("content", "")).startswith(LEDGER_TAG)]
    assert len(ledgers) == 1 and "x.sv" in ledgers[0]["content"]
    assert "J-0001 passed" in ledgers[0]["content"] and ledgers[0]["guard"] == "ledger"
    led.strip(t)
    assert all(not str(m.get("content", "")).startswith(LEDGER_TAG) for m in t)


def _lint(job, errors):
    return {"job": job, "status": "passed" if errors == 0 else "failed", "errors": errors}


def test_tracker_goes_green_and_checkpoints_the_strongest_pass():
    ver = VerificationTracker()
    saved = []
    key_f, key_t = ("lint.run", "files"), ("lint.run", "files+top")
    assert ver.observe("lint.run", key_f, "files", 0, _lint("J-1", 0), 1, on_green=saved.append) == []
    assert ver.is_green(1) and ver.green_job == "J-1" and saved == ["J-1"]
    # a stronger scope fails on the same tree: not green, the gap is taught,
    # the weaker pass stays on record
    out = ver.observe("lint.run", key_t, "files+top", 1, _lint("J-2", 15), 1, on_green=saved.append)
    assert [t for t, _ in out] == ["scope_gap"] and "J-2 FAILED on the SAME tree with 15 errors" in out[0][1]
    assert not ver.is_green(1) and ver.fails_since_green == 1
    assert "J-1 passed" in ver.summary(1) and "FAILED (15 errors)" in ver.summary(1)
    # repeating the already-current weaker check is called out once
    out = ver.observe("lint.run", key_f, "files", 0, _lint("J-3", 0), 1, on_green=saved.append)
    assert [t for t, _ in out] == ["scope_redo"]
    assert ver.observe("lint.run", key_f, "files", 0, _lint("J-4", 0), 1, on_green=saved.append) == []
    # a new epoch (the tree changed) forgets nothing but reports fresh
    assert ver.summary(2).startswith("none yet")


def test_tracker_ignores_results_without_a_verdict():
    ver = VerificationTracker()
    assert ver.observe("sim.run", ("sim.run", "t"), "t", 2, {"status": "running"}, 0,
                       on_green=lambda j: None) == []
    assert ver.checks == {}


def test_run_state_tracks_the_bad_oracle_signature():
    st = RunState()
    sig = ("fifo_test", "mismatch at 120ns")
    for _ in range(3):
        st.saw_sim_signature(sig, job_passed=False)
    assert st.sim_sig_count == 3 and st.oracle_suspect()
    st.sim_sigs_nudged.add(sig)
    assert not st.oracle_suspect()                  # nudged once, never again
    st.saw_sim_signature(None, job_passed=True)
    assert st.sim_sig is None and st.sim_sig_count == 0
