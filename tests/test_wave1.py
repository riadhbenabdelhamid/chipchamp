"""Wave 1 — job cache, undo, budget enforcement, progressive tool disclosure.

Each of these closes a gap where the substrate already existed and only the
action was missing: inputs_hash was computed and never looked up, ctx.edits held
an inverse patch nothing could apply, the budget ledger had checkers with no
call sites, and every request shipped all 91 tool schemas.
"""
from __future__ import annotations

import pytest

from chipchamp.adapters.base import (Adapter, NormalizedDiagnostic, Plan, Step,
                                     ToolResult)
from chipchamp.agent import disclosure
from chipchamp.agent.loop import AgentLoop
from chipchamp.jobs.cache import cache_key, result_from_record, reusable
from chipchamp.jobs.runner import JobRunner
from chipchamp.policy.budgets import Budget, BudgetExceeded, BudgetLedger


# ---- fixtures ---------------------------------------------------------------

class _Adapter(Adapter):
    name = "fake"
    kinds = ("lint",)

    def __init__(self, status="passed", version="1.0"):
        self._status = status
        self._version = version
        self.parsed = 0

    def version(self):
        return self._version

    def available(self):
        return True

    def plan(self, **kw):
        raise NotImplementedError

    def parse(self, plan, results):
        self.parsed += 1
        return ToolResult(ok=self._status == "passed", kind="lint",
                          adapter=self.name, status=self._status,
                          summary=f"parse #{self.parsed}",
                          diagnostics=[NormalizedDiagnostic(
                              tool="fake", severity="error", message="boom",
                              file="a.sv", line=7)],
                          metrics={"errors": 1})


def _plan(tmp_path, arg="x"):
    return Plan(kind="lint", adapter="fake", workdir=str(tmp_path),
                steps=[Step(argv=["true", arg], cwd=str(tmp_path))])


@pytest.fixture
def runner(tmp_path):
    return JobRunner(str(tmp_path / "runs"), registry=None)


@pytest.fixture
def src(tmp_path):
    f = tmp_path / "a.sv"
    f.write_text("module a; endmodule\n")
    return [str(f)]


# ---- B: the job cache -------------------------------------------------------

def test_identical_inputs_reuse_the_record_instead_of_rerunning(runner, tmp_path, src):
    ad = _Adapter()
    rec1, res1 = runner.submit(_plan(tmp_path), ad, input_files=src)
    assert ad.parsed == 1
    rec2, res2 = runner.submit(_plan(tmp_path), ad, input_files=src)
    # nothing re-ran, and the ORIGINAL record came back — the evidence a gate
    # validates has to be the real job, not a copy that claims to have run
    assert ad.parsed == 1
    assert rec2.id == rec1.id and rec2.cached is True
    assert res2.status == res1.status and res2.metrics == res1.metrics
    assert [d.message for d in res2.diagnostics] == ["boom"]


def test_editing_an_input_misses_the_cache(runner, tmp_path, src):
    """hash_manifest hashes bytes, so a one-character edit is a different job."""
    ad = _Adapter()
    runner.submit(_plan(tmp_path), ad, input_files=src)
    (tmp_path / "a.sv").write_text("module a; wire w; endmodule\n")
    rec, _ = runner.submit(_plan(tmp_path), ad, input_files=src)
    assert ad.parsed == 2 and rec.cached is False


def test_a_new_tool_version_misses_the_cache(runner, tmp_path, src):
    runner.submit(_plan(tmp_path), _Adapter(version="1.0"), input_files=src)
    rec, _ = runner.submit(_plan(tmp_path), _Adapter(version="2.0"),
                           input_files=src)
    assert rec.cached is False


def test_seed_and_argv_are_part_of_the_key(runner, tmp_path, src):
    ad = _Adapter()
    runner.submit(_plan(tmp_path), ad, input_files=src, seed=1)
    assert runner.submit(_plan(tmp_path), ad, input_files=src,
                         seed=2)[0].cached is False
    assert runner.submit(_plan(tmp_path, arg="y"), ad,
                         input_files=src)[0].cached is False


def test_a_referenced_file_outside_the_manifest_still_keys_the_cache(tmp_path):
    """A generated testbench under work/ is usually not in the target's
    manifest. Without hashing what argv names, editing it would silently reuse
    the old verdict — the worst possible cache bug in a verification tool."""
    tb = tmp_path / "tb.sv"
    tb.write_text("initial begin end\n")
    ad = _Adapter()
    p = Plan(kind="lint", adapter="fake", workdir=str(tmp_path),
             steps=[Step(argv=["verilator", str(tb)], cwd=str(tmp_path))])
    k1 = cache_key(p, ad, inputs_hash="same")
    tb.write_text("initial begin $finish; end\n")
    assert cache_key(p, ad, inputs_hash="same") != k1


def test_the_jobs_own_outputs_are_not_part_of_its_key(tmp_path):
    """argv names outputs as well as inputs (`iverilog -o …tb.vvp`). Hashing
    those makes the key depend on the job's own product, which the job rewrites
    every run — so the key never stabilises and the entry is never reachable.

    This is why `sim` passed every unit test and never once hit in a real
    workspace: the test plans here declared no artifacts, so the bug was
    invisible until a real iverilog plan was run three times in a row.
    """
    out = tmp_path / "tb.vvp"
    inp = tmp_path / "tb.sv"
    inp.write_text("module tb; endmodule\n")
    out.write_text("compiled-v1")
    ad = _Adapter()
    p = Plan(kind="sim", adapter="fake", workdir=str(tmp_path),
             steps=[Step(argv=["iverilog", "-o", str(out), str(inp)],
                         cwd=str(tmp_path))],
             artifacts={"vvp": str(out)})
    k1 = cache_key(p, ad, inputs_hash="same")
    out.write_text("compiled-v2")          # the job rewrote its own output
    assert cache_key(p, ad, inputs_hash="same") == k1
    inp.write_text("module tb; wire w; endmodule\n")   # a real input changed
    assert cache_key(p, ad, inputs_hash="same") != k1


def test_errors_and_timeouts_are_never_reused(tmp_path):
    """Those describe the environment on a bad day, not the inputs. Caching a
    fluke would make it permanent; retrying is the correct response."""
    from chipchamp.jobs.record import JobRecord
    rec = JobRecord(id="J-1", kind="lint", adapter="fake", status="error",
                    input_files=["a"], result={"ok": False})
    assert reusable(rec, tmp_path) is False
    rec.status = "timeout"
    assert reusable(rec, tmp_path) is False
    rec.status = "passed"
    assert reusable(rec, tmp_path) is True
    rec.input_files = []          # nothing hashed → the key means nothing
    assert reusable(rec, tmp_path) is False


def test_only_verdict_carrying_kinds_are_reused(tmp_path):
    """A synth writes a mapped netlist the next step reads by path. Skipping
    the run skips the file, and the downstream tool then measures nothing and
    reports zero — worse than a slow answer, because it looks like data.
    (Caught by test_optimize_loop when this rule was missing.)"""
    from chipchamp.jobs.cache import CACHEABLE_KINDS
    from chipchamp.jobs.record import JobRecord
    for kind in ("synth", "sta", "lec", "formal", "coverage", "pnr"):
        rec = JobRecord(id="J-1", kind=kind, adapter="a", status="passed",
                        input_files=["x"], result={"ok": True})
        assert reusable(rec, tmp_path) is False, kind
    assert "lint" in CACHEABLE_KINDS and "sim" in CACHEABLE_KINDS


def test_a_hit_requires_its_artifacts_to_still_exist(tmp_path):
    """The waves a failing sim dumped are what the next step opens."""
    from chipchamp.jobs.record import JobRecord
    waves = tmp_path / "dump.vcd"
    rec = JobRecord(id="J-1", kind="sim", adapter="a", status="failed",
                    input_files=["x"], result={"ok": False},
                    artifacts={"waves": str(waves)})
    assert reusable(rec, tmp_path) is False      # not written yet
    waves.write_text("$date")
    assert reusable(rec, tmp_path) is True


def test_a_hit_requires_the_new_plans_artifacts_too(tmp_path):
    """The caller will open the paths the FRESH plan declared, not the old
    record's — if they are not there, the run has to happen."""
    from chipchamp.jobs.record import JobRecord
    rec = JobRecord(id="J-1", kind="sim", adapter="a", status="passed",
                    input_files=["x"], result={"ok": True})
    plan = Plan(kind="sim", adapter="a", workdir=str(tmp_path), steps=[],
                artifacts={"waves": str(tmp_path / "new.vcd")})
    assert reusable(rec, tmp_path, plan) is False
    (tmp_path / "new.vcd").write_text("$date")
    assert reusable(rec, tmp_path, plan) is True


def test_a_deleted_job_directory_is_not_evidence(runner, tmp_path, src):
    import shutil
    ad = _Adapter()
    rec, _ = runner.submit(_plan(tmp_path), ad, input_files=src)
    shutil.rmtree(tmp_path / "runs" / rec.id)
    assert runner.submit(_plan(tmp_path), ad, input_files=src)[0].cached is False


def test_the_cache_can_be_turned_off(tmp_path, src):
    r = JobRunner(str(tmp_path / "runs"), registry=None, cache=False)
    ad = _Adapter()
    r.submit(_plan(tmp_path), ad, input_files=src)
    r.submit(_plan(tmp_path), ad, input_files=src)
    assert ad.parsed == 2


def test_no_cache_forces_a_rerun(runner, tmp_path, src):
    ad = _Adapter()
    runner.submit(_plan(tmp_path), ad, input_files=src)
    rec, _ = runner.submit(_plan(tmp_path), ad, input_files=src, no_cache=True)
    assert ad.parsed == 2 and rec.cached is False


def test_result_from_record_survives_a_missing_result(tmp_path):
    from chipchamp.jobs.record import JobRecord
    assert result_from_record(JobRecord(id="J", kind="lint", adapter="f")) is None


# ---- C: budget enforcement --------------------------------------------------

def test_the_token_ceiling_actually_holds():
    led = BudgetLedger(budget=Budget(model_tokens=100))
    assert led.can_spend_tokens()[0] is True
    led.record_tokens(150)
    ok, why = led.can_spend_tokens()
    assert ok is False and "budget exhausted" in why


def test_an_exhausted_license_budget_blocks_the_job(tmp_path, monkeypatch):
    from chipchamp.tools.context import ToolContext
    ctx = ToolContext.__new__(ToolContext)
    ctx.ledger = BudgetLedger(budget=Budget(license_hours=1.0))
    ctx.ledger.license_seconds = 7200.0        # already 2h in
    ctx.budget_approver = lambda kind, why: False
    plan = Plan(kind="sim", adapter="f", workdir=".", steps=[],
                license_features=["questa"])
    with pytest.raises(BudgetExceeded):
        ctx.submit(plan, _Adapter())


def test_a_human_may_authorize_the_overrun(tmp_path, src):
    """The docstring always promised a pause that ASKS. Approval proceeds."""
    from chipchamp.tools.context import ToolContext
    ctx = ToolContext.__new__(ToolContext)
    ctx.ledger = BudgetLedger(budget=Budget(license_hours=1.0))
    ctx.ledger.license_seconds = 7200.0
    ctx.budget_approver = lambda kind, why: True
    ctx.task_jobs = []
    ctx.log_lines = []
    ctx.ws = type("W", (), {"config": {}})()
    ctx.target = type("T", (), {"sources": src})()
    ctx.runner = JobRunner(str(tmp_path / "runs"), registry=None)
    plan = Plan(kind="lint", adapter="fake", workdir=str(tmp_path),
                steps=[Step(argv=["true"], cwd=str(tmp_path))],
                license_features=["questa"])
    rec, _ = ctx.submit(plan, _Adapter())      # must not raise
    assert rec.status == "passed"


def test_a_cache_hit_is_not_billed(tmp_path, src):
    from chipchamp.tools.context import ToolContext
    ctx = ToolContext.__new__(ToolContext)
    ctx.ledger = BudgetLedger()
    ctx.budget_approver = lambda kind, why: False
    ctx.task_jobs = []
    ctx.log_lines = []
    ctx.ws = type("W", (), {"config": {}})()
    ctx.target = type("T", (), {"sources": src})()
    ctx.runner = JobRunner(str(tmp_path / "runs"), registry=None)
    ad = _Adapter()
    ctx.submit(_plan(tmp_path), ad)
    spent = ctx.ledger.cpu_seconds
    rec, _ = ctx.submit(_plan(tmp_path), ad)
    assert rec.cached is True
    assert ctx.ledger.cpu_seconds == spent      # reuse costs nothing
    assert any("reused" in m for m in ctx.log_lines)   # and says so


# ---- D: undo ----------------------------------------------------------------

def _ctx_for_undo(tmp_path):
    from chipchamp.tools.context import ToolContext
    ctx = ToolContext.__new__(ToolContext)
    ctx.ws = type("W", (), {"root": str(tmp_path)})()
    ctx.edits = {}
    ctx.undo_stack = []
    return ctx


def test_undo_restores_modified_files_and_removes_created_ones(tmp_path):
    (tmp_path / "keep.sv").write_text("original\n")
    ctx = _ctx_for_undo(tmp_path)
    ctx.checkpoint("task one")
    (tmp_path / "keep.sv").write_text("edited\n")
    ctx.record_edit("keep.sv", "original\n", "edited\n", "modified")
    (tmp_path / "new.sv").write_text("brand new\n")
    ctx.record_edit("new.sv", "", "brand new\n", "added")

    out = ctx.undo()
    assert out["restored"] == ["keep.sv"] and out["deleted"] == ["new.sv"]
    assert (tmp_path / "keep.sv").read_text() == "original\n"
    assert not (tmp_path / "new.sv").exists()


def test_undo_never_discards_a_humans_own_edit(tmp_path):
    """If the file no longer matches what the agent last wrote, somebody
    changed it by hand. Overwriting that would be the worst possible undo."""
    (tmp_path / "a.sv").write_text("original\n")
    ctx = _ctx_for_undo(tmp_path)
    ctx.checkpoint("t")
    (tmp_path / "a.sv").write_text("agent version\n")
    ctx.record_edit("a.sv", "original\n", "agent version\n", "modified")
    (tmp_path / "a.sv").write_text("human went in and fixed it\n")

    out = ctx.undo()
    assert out["skipped"] == ["a.sv"] and out["restored"] == []
    assert (tmp_path / "a.sv").read_text() == "human went in and fixed it\n"


def test_undo_is_scoped_to_one_task(tmp_path):
    """Two tasks, one undo: only the second task's work comes back."""
    ctx = _ctx_for_undo(tmp_path)
    ctx.checkpoint("first")
    (tmp_path / "a.sv").write_text("from task one\n")
    ctx.record_edit("a.sv", "", "from task one\n", "added")
    ctx.checkpoint("second")
    (tmp_path / "a.sv").write_text("from task two\n")
    ctx.record_edit("a.sv", "", "from task two\n", "modified")

    out = ctx.undo()
    assert out["checkpoint"] == "second"
    assert (tmp_path / "a.sv").read_text() == "from task one\n"


def test_undo_with_nothing_to_undo_says_so(tmp_path):
    assert "error" in _ctx_for_undo(tmp_path).undo()


# ---- 1b: progressive tool disclosure ----------------------------------------

def test_the_core_set_covers_the_bottom_of_the_ladder():
    from chipchamp.tools import all_tools
    core = disclosure.core_names(all_tools())
    for must in ("fs.read", "fs.write", "design.hierarchy", "plan.update",
                 "lint.run", "sim.run", "job.log", "report.done", "caps",
                 "policy.check", "tools.load"):
        assert must in core, must
    # and the expensive verticals are NOT paid for up front
    for deferred in ("pd.sweep", "efpga-fabulous.fabric", "riscv.cosim",
                     "wave.compare", "fpga.bitstream"):
        assert deferred not in core, deferred


def test_disclosure_shrinks_what_is_sent_and_load_widens_it(tmp_path):
    from chipchamp.tools import all_tools
    ctx = type("C", (), {})()
    gw = type("G", (), {"available": True, "ref": "ollama:x"})()
    loop = AgentLoop.__new__(AgentLoop)
    loop.ctx, loop.gateway = ctx, gw
    loop.tools = all_tools()
    loop.on_event = lambda k, d: None
    loop._name_map = {n.replace(".", "__"): n for n in loop.tools}
    loop.disclose = True
    loop._loaded = disclosure.core_names(loop.tools)
    loop._prompt_dirty = False
    loop._refresh_api_tools()

    lean = len(loop._api_tools)
    assert lean < len(loop.tools) / 2          # the point of the exercise
    assert not any(t["name"].startswith("wave__") for t in loop._api_tools)

    out = loop.load_groups(["wave"])
    assert "wave.compare" in out["loaded"]
    assert any(t["name"] == "wave__compare" for t in loop._api_tools)
    assert len(loop._api_tools) > lean
    assert loop._prompt_dirty is True          # the menu must be re-rendered


def test_an_unknown_group_is_reported_with_the_real_ones(tmp_path):
    from chipchamp.tools import all_tools
    loop = AgentLoop.__new__(AgentLoop)
    loop.tools = all_tools()
    loop.on_event = lambda k, d: None
    loop.disclose = True
    loop._loaded = disclosure.core_names(loop.tools)
    loop._prompt_dirty = False
    loop._refresh_api_tools()
    out = loop.load_groups(["waves"])          # plural typo
    assert out["unknown_groups"] == ["waves"]
    assert "wave" in out["known_groups"]


def test_a_correctly_named_unloaded_tool_still_resolves():
    """Narrowing the schemas sent must not narrow what CAN be called: a model
    that names a deferred tool correctly should have it work."""
    from chipchamp.tools import all_tools
    loop = AgentLoop.__new__(AgentLoop)
    loop.tools = all_tools()
    loop._name_map = {n.replace(".", "__"): n for n in loop.tools}
    loop.disclose = True
    loop._loaded = disclosure.core_names(loop.tools)
    assert loop._name_map["pd__sweep"] == "pd.sweep"
    assert "pd.sweep" in loop.tools


def test_the_deferred_menu_lists_names_without_schemas():
    from chipchamp.tools import all_tools
    t = all_tools()
    menu = disclosure.deferred_summary(t, disclosure.core_names(t))
    assert "[wave]" in menu and "wave.compare" in menu
    assert "properties" not in menu           # names only — that is the trick
    assert len(menu) < 2000


def test_a_cached_pass_outranks_the_failure_it_followed(tmp_path):
    """The fix-then-rerun flow, with the job cache in play.

    A fix that restores a file to a state already simulated once produces a
    CACHE HIT: the original record comes back, carrying its original start_ts.
    Ranking sims by wall-clock then put that just-delivered pass *below* the
    failure that preceded it, and report.done rejected a fix that worked.
    Caught by demo.py, which the unit suite never runs.
    """
    from chipchamp.policy.gates import Evidence, _latest_sims
    from chipchamp.jobs.record import JobRecord

    def sim(jid, status, start_ts):
        return JobRecord(id=jid, kind="sim", adapter="icarus", status=status,
                         start_ts=start_ts, seed=1,
                         result={"status": "pass" if status == "passed" else "fail"},
                         artifacts={"waves": "w.vcd"},
                         defines={}, input_files=["tb.sv"])

    failed = sim("J-0066", "failed", start_ts=9000.0)     # ran just now
    cached = sim("J-0002", "passed", start_ts=10.0)       # served from cache
    # submission order: the failure first, then the cached pass after the fix
    latest = _latest_sims(Evidence(jobs=[failed, cached]))
    assert [j.id for j in latest] == ["J-0002"], "the cached pass must win"

    # and the ordinary case is unchanged: a later failure still beats an
    # earlier pass, so a regression cannot hide behind a stale success
    latest = _latest_sims(Evidence(jobs=[cached, failed]))
    assert [j.id for j in latest] == ["J-0066"]


def test_a_clobbered_artifact_invalidates_the_hit(tmp_path, src):
    """Simulators write waves to a FIXED workspace path, not a per-job one, so
    a later run of a different design overwrites the file a cached record still
    points at. Checking only that the path EXISTS let a hit hand back another
    run's waveform — the agent debugging the wrong data and never knowing.

    Caught by demo.py: its golden-vs-failing comparison found no divergence at
    all, because the "golden" copy was really the failing run's waves.
    """
    waves = tmp_path / "tb.vcd"
    waves.write_text("$date golden $end")

    class _WaveAdapter(_Adapter):
        def parse(self, plan, results):
            r = super().parse(plan, results)
            r.artifacts = {"waves": str(waves)}
            return r

    r = JobRunner(str(tmp_path / "runs"), registry=None)
    p = Plan(kind="sim", adapter="fake", workdir=str(tmp_path),
             steps=[Step(argv=["true"], cwd=str(tmp_path))])
    ad = _WaveAdapter()
    first, _ = r.submit(p, ad, input_files=src)
    assert r.submit(p, ad, input_files=src)[0].cached is True   # bytes intact

    waves.write_text("$date SOME OTHER RUN $end")               # clobbered
    rec, _ = r.submit(p, ad, input_files=src)
    assert rec.cached is False, "a rewritten artifact must force a re-run"
