"""Wave 2 — the gate ladder as a live HUD, and test history that outlives a run.

Both take something the platform already computed and make it visible: the
ladder was calculated only at report.done, and failure signatures were clustered
inside one regression call and thrown away.
"""
from __future__ import annotations

from chipchamp import ui
from chipchamp.jobs.history import TestHistory
from chipchamp.policy.gates import GateReport, GateResult


def _report(**status):
    r = GateReport(task_class="rtl_functional", min_rung="V3")
    r.gates = [GateResult(name=n, status=s) for n, s in status.items()]
    return r


# ---- #3: the ladder HUD -----------------------------------------------------

def test_the_ladder_shows_every_gate_and_the_count():
    line = ui.gate_ladder(_report(lint="pass", smoke_sim="pass",
                                  affected_regress="missing", no_gaming="pass"))
    assert "V3" in line and "rtl_functional" in line
    assert "3/4" in line
    for name in ("lint", "smoke_sim", "affected_regress", "no_gaming"):
        assert name in line          # verbatim: these are report.done's words


def test_gate_states_are_visually_distinct():
    line = ui.gate_ladder(_report(lint="pass", smoke_sim="fail",
                                  affected_regress="missing"))
    assert "[green]✓[/]" in line and "[bold red]✗[/]" in line
    assert "[grey42]○[/]" in line
    # a failing gate's NAME shouts too — it is report.done's rejection word
    assert "[red]smoke_sim[/]" in line


def test_a_complete_ladder_reads_green():
    assert "[green]2/2[/]" in ui.gate_ladder(_report(lint="pass", no_gaming="pass"))
    # a ladder with a FAILING gate counts in red, not yellow — yellow is
    # reserved for merely-unpaid (missing) gates
    assert "[red]1/2[/]" in ui.gate_ladder(_report(lint="pass",
                                                   no_gaming="fail"))
    assert "[yellow]1/2[/]" in ui.gate_ladder(_report(lint="pass",
                                                      no_gaming="missing"))


def test_the_ladder_is_one_line():
    """It repaints after every job — a block would push the conversation off
    screen within a few steps."""
    assert "\n" not in ui.gate_ladder(
        _report(lint="pass", smoke_sim="pass", affected_regress="missing",
                coverage_baseline="missing", no_gaming="pass"))


def test_no_report_renders_nothing_rather_than_a_stub():
    assert ui.gate_ladder(None) == ""
    assert ui.gate_ladder(_report()) == ""
    assert ui.gate_fingerprint(None) == ""


def test_the_fingerprint_changes_only_when_a_gate_moves():
    a = _report(lint="pass", smoke_sim="missing")
    b = _report(lint="pass", smoke_sim="missing")
    c = _report(lint="pass", smoke_sim="pass")
    assert ui.gate_fingerprint(a) == ui.gate_fingerprint(b)   # no repaint
    assert ui.gate_fingerprint(a) != ui.gate_fingerprint(c)   # repaint


def test_gate_status_reads_the_same_evidence_report_done_will(tmp_path):
    """The ladder a human watches has to be the ladder the task is judged by —
    an optimistic HUD would be worse than none."""
    from chipchamp.tools.context import ToolContext
    calls = {}

    ctx = ToolContext.__new__(ToolContext)
    ctx.edits = {"a.sv": {"old": "", "new": "x", "status": "modified"}}
    ctx.declared_nfc = ctx.declared_cdc = ctx.declared_timing = False
    ctx.closure_claim = False
    ctx.task_jobs = []
    ctx.coverage_sets = {}
    ctx.acked_findings = set()
    ctx.ws = type("W", (), {"config": {}, "generated": [],
                            "coverage_baseline": lambda self=None: None})()
    ctx.sta_delta = ctx.riscv_cosim = ctx.riscv_compliance = None

    class _P:
        def validate_done(self, diff, ev, closure_claim=False):
            calls["diff"] = diff
            return _report(lint="missing")
    ctx.policy = _P()
    rep = ctx.gate_status()
    assert rep.gates[0].name == "lint"
    assert [f.path for f in calls["diff"].files] == ["a.sv"]


def test_a_broken_gate_evaluation_never_breaks_the_run(tmp_path):
    from chipchamp.tools.context import ToolContext
    ctx = ToolContext.__new__(ToolContext)
    ctx.policy = type("P", (), {"validate_done": lambda *a, **k: 1 / 0})()
    ctx.edits = {}
    assert ctx.gate_status() is None      # a HUD must not be able to fail a task


# ---- E: history across runs -------------------------------------------------

def test_the_same_seed_disagreeing_is_flaky(tmp_path):
    h = TestHistory(tmp_path)
    h.record("fifo_smoke", 7, "passed")
    h.record("fifo_smoke", 7, "failed", signature="assert fifo.sv:31")
    v = h.verdict("fifo_smoke", 7)
    assert v["verdict"] == "flaky" and v["flake_rate"] == 0.5
    assert v["signatures"] == ["assert fifo.sv:31"]


def test_different_seeds_disagreeing_is_not_flaky(tmp_path):
    """A test that fails at seed 3 and passes at seed 4 found a bug at seed 3.
    Calling that flaky is how real failures get ignored."""
    h = TestHistory(tmp_path)
    h.record("fifo_smoke", 3, "failed")
    h.record("fifo_smoke", 3, "failed")
    h.record("fifo_smoke", 4, "passed")
    h.record("fifo_smoke", 4, "passed")
    assert h.verdict("fifo_smoke", 3)["verdict"] == "failing"
    assert h.verdict("fifo_smoke", 4)["verdict"] == "stable"
    assert h.flaky_tests() == []


def test_one_run_is_not_yet_a_verdict(tmp_path):
    h = TestHistory(tmp_path)
    h.record("t", 1, "failed")
    assert h.verdict("t", 1)["verdict"] == "unknown"


def test_history_survives_the_process(tmp_path):
    """The whole point: last night's outcome has to be readable tonight."""
    TestHistory(tmp_path).record("t", 1, "passed")
    TestHistory(tmp_path).record("t", 1, "failed")
    assert TestHistory(tmp_path).verdict("t", 1)["verdict"] == "flaky"


def test_history_is_bounded_per_key(tmp_path):
    h = TestHistory(tmp_path, limit_per_key=5)
    for i in range(20):
        h.record("t", 1, "passed", save=False)
    assert len(h.runs("t", 1)) == 5        # a year of nightlies stays readable


def test_flaky_tests_are_ranked_by_rate(tmp_path):
    h = TestHistory(tmp_path)
    for st in ("passed", "failed", "failed", "failed"):
        h.record("bad", 1, st)
    for st in ("passed", "passed", "passed", "failed"):
        h.record("mild", 1, st)
    assert [f["test"] for f in h.flaky_tests()] == ["bad", "mild"]


def test_a_corrupt_history_file_degrades_to_empty(tmp_path):
    (tmp_path / "history.json").write_text("{not json")
    h = TestHistory(tmp_path)
    assert h.summary()["tracked"] == 0
    h.record("t", 1, "passed")            # and still records from here
    assert h.runs("t", 1)


def test_a_cache_hit_is_not_a_second_observation(tmp_path):
    """Recording a reused verdict would manufacture agreement with itself and
    make a genuinely flaky test look stable."""
    import inspect

    from chipchamp.tools import verif_tools
    src = inspect.getsource(verif_tools.sim_run)
    assert 'if not getattr(rec, "cached", False):' in src
    assert "ctx.history.record" in src


def test_job_log_on_a_nonexistent_job_errors_instead_of_empty_hits(tmp_path):
    """A live 4B passed the TEST NAME as a job id and got {"hits": []} —
    indistinguishable from "the job exists and nothing matched" — so it
    believed it had consulted a log that was never read. A miss must be
    distinguishable from a match-less grep, and the error should teach the
    id shape rather than just refuse."""
    from chipchamp.tools import all_tools

    class _Runner:
        def get(self, job):
            return None
        def list_jobs(self):
            return [type("R", (), {"id": "J-0007"})()]

    ctx = type("C", (), {"runner": _Runner()})()
    out = all_tools()["job.log"].handler(ctx, job="fifo_smoke", pattern="FAIL")
    assert "no job 'fifo_smoke'" in out["error"]
    assert "J-0249" in out["error"] or "J-0" in out["error"]   # teaches the shape
    assert "J-0007" in out["error"]                            # and names real ones
