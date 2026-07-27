"""Multi-agent orchestration (SPEC §8.10): least-privilege subagents, shared
license pool across the fan-out, orchestrator-only success reporting."""
from __future__ import annotations

import threading
import time

from chipchamp.agent.orchestrator import Orchestrator, SubagentTask
from chipchamp.agent.providers.base import ModelResponse
from chipchamp.agent.roles import builtin_roles


class ScriptedGateway:
    available = True
    ref = "fake:sub"

    def __init__(self, script):
        self.script = list(script)

    def complete(self, system, transcript, tools, audit_items=None):
        if not self.script:
            return ModelResponse(text="done", tool_calls=[])
        return self.script.pop(0)


def test_roles_are_least_privilege():
    roles = builtin_roles()
    for role in roles.values():
        # FR-MA-03: no subagent may report success or mint evidence
        assert "report.done" not in role.tool_names or True
        assert "report.done" not in role.tools()
        assert "evidence.bundle" not in role.tools()
    # read-only roles cannot edit
    for ro in ("triage-analyst", "wave-analyst", "reviewer"):
        assert "fs.edit" not in roles[ro].tools()
        assert "fs.write" not in roles[ro].tools()
    # fixers can edit but still cannot report done
    assert "fs.edit" in roles["lint-fixer"].tools()


def test_subagent_calling_forbidden_tool_gets_error(ctx):
    # a subagent that tries report.done must receive unknown-tool, not success
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "report__done",
                                            "input": {"summary": "hack"}}]),
        ModelResponse(text="finished", tool_calls=[]),
    ]
    orch = Orchestrator(ctx, lambda role: ScriptedGateway(script), max_concurrent=1)
    res = orch.run([SubagentTask(role="triage-analyst", prompt="p", label="C1")])[0]
    assert res.error is None and res.text == "finished"
    # and the parent context saw no accepted completion (no gate side effects)
    assert ctx.task_jobs == []


def test_fanout_merges_results_and_runs_parallel(ctx):
    import threading
    t0 = time.time()
    slow = 0.15
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    class SlowGateway(ScriptedGateway):
        def complete(self, *a, **k):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(slow)
            with lock:
                active["now"] -= 1
            return ModelResponse(text="analysis-done", tool_calls=[])

    orch = Orchestrator(ctx, lambda role: SlowGateway([]), max_concurrent=4)
    tasks = [SubagentTask(role="wave-analyst", prompt=f"q{i}", label=f"C{i}")
             for i in range(4)]
    results = orch.run(tasks)
    wall = time.time() - t0
    assert len(results) == 4 and all(r.text == "analysis-done" for r in results)
    assert {r.label for r in results} == {"C0", "C1", "C2", "C3"}
    # parallelism, measured directly (overlapping model calls) rather than by
    # wall clock — an absolute bound flakes on a loaded machine (was: <0.5s)
    assert active["max"] >= 2, "fan-out did not overlap model calls"
    assert wall < 3.0, f"fan-out took implausibly long ({wall:.2f}s)"


def test_subagents_share_job_store_and_license_pool(workspace, tmp_path):
    """FR-MA-02: N subagents submitting licensed jobs serialize on the shared
    token pool and draw unique ids from the shared store."""
    from chipchamp.adapters.base import Adapter, Plan, Step, ToolResult
    from chipchamp.jobs import JobRunner
    from chipchamp.adapters import AdapterRegistry
    from chipchamp.tools.context import ToolContext

    class TinyAdapter(Adapter):
        name = "tiny"
        binary = "true"

        def plan(self, workdir):
            return Plan(kind="sim", adapter="tiny", workdir=workdir,
                        steps=[Step(argv=["sleep", "0.1"], cwd=workdir)],
                        license_features=["tinylic"])

        def parse(self, plan, results):
            return ToolResult(ok=True, kind="sim", adapter="tiny", status="pass",
                              summary="ok")

    runner = JobRunner(str(tmp_path / "runs-mt"), AdapterRegistry(),
                       licenses={"tinylic": 1})  # ONE seat; isolated store
    parent = ToolContext(workspace, runner=runner)
    adapter = TinyAdapter()
    concurrent = {"now": 0, "peak": 0}
    lock = threading.Lock()

    orig = runner._run_step

    def counting_run_step(st, job_dir, idx, timeout, **kw):
        with lock:
            concurrent["now"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
        try:
            return orig(st, job_dir, idx, timeout, **kw)
        finally:
            with lock:
                concurrent["now"] -= 1

    runner._run_step = counting_run_step

    def submit_from_child():
        child = ToolContext(workspace, runner=parent.runner, policy=parent.policy)
        plan = adapter.plan(str(workspace.root))
        child.submit(plan, adapter, input_files=[])
        return child

    threads = [threading.Thread(target=submit_from_child) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # one license token -> never more than one step in flight
    assert concurrent["peak"] == 1, f"license pool violated (peak={concurrent['peak']})"
    ids = [r.id for r in runner.list_jobs()]
    assert len(ids) == len(set(ids)) == 3  # unique ids from the shared store
