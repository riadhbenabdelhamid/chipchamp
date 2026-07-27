"""Multi-agent orchestration (SPEC §8.10).

Deterministic fan-out of typed subagents with the orchestrator owning synthesis:

- **FR-MA-01** — each subagent gets a least-privilege tool subset from its Role.
- **FR-MA-02** — all subagents share the parent's JobRunner (one job store, one
  license-token pool, one budget ledger), so a 40-wide fan-out cannot consume 40
  licensed seats: licensed jobs queue on the shared semaphores. A `max_concurrent`
  cap bounds model-call parallelism as well.
- **FR-MA-03** — only the orchestrator can report success: subagent tool sets
  exclude ``report.done``/``evidence.bundle`` structurally, and subagent job
  records + edits are merged back into the parent context, where the parent's
  gate validation runs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..tools.context import ToolContext
from .loop import AgentLoop
from .roles import Role, builtin_roles


@dataclass
class SubagentTask:
    role: str
    prompt: str
    label: str = ""


@dataclass
class SubagentResult:
    label: str
    role: str
    text: str
    steps: int = 0
    jobs: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    error: Optional[str] = None


class Orchestrator:
    def __init__(self, ctx: ToolContext, gateway_factory: Callable[[str], object],
                 max_concurrent: int = 4, max_steps_cap: int = 25,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        """`gateway_factory(role_name)` returns a gateway per subagent — this is
        where model routing (§14) plugs in (e.g. small model for wave-analysts)."""
        self.ctx = ctx
        self.gateway_factory = gateway_factory
        self.max_concurrent = max(1, max_concurrent)
        self.max_steps_cap = max_steps_cap
        self.roles = builtin_roles()
        self.on_event = on_event or (lambda k, d: None)

    def _child_context(self) -> ToolContext:
        # share runner (job store + license pool) and policy (ledger + ACL)
        return ToolContext(self.ctx.ws, target=self.ctx.target_name,
                           runner=self.ctx.runner, policy=self.ctx.policy)

    def _run_one(self, task: SubagentTask) -> SubagentResult:
        role = self.roles.get(task.role)
        if role is None:
            return SubagentResult(label=task.label, role=task.role, text="",
                                  error=f"unknown role '{task.role}'")
        child = self._child_context()
        loop = AgentLoop(
            child, self.gateway_factory(role.name),
            max_steps=min(role.max_steps, self.max_steps_cap),
            tools=role.tools(), system_suffix=role.system_suffix,
            on_event=lambda k, d, _l=task.label: self.on_event(k, {**d, "subagent": _l}))
        try:
            out = loop.run(task.prompt)
        except Exception as e:  # a crashed subagent must not kill the fan-out
            return SubagentResult(label=task.label, role=role.name, text="",
                                  error=f"{type(e).__name__}: {e}")
        # merge evidence back into the parent (FR-MA-03: parent owns gates)
        self.ctx.task_jobs.extend(child.task_jobs)
        for path, e in child.edits.items():
            base = self.ctx.edits.get(path)
            self.ctx.record_edit(path, base["old"] if base else e["old"],
                                 e["new"], e["status"])
        return SubagentResult(
            label=task.label, role=role.name, text=out.get("text", ""),
            steps=out.get("steps", 0), jobs=out.get("jobs", []),
            edited=sorted(child.edits),
            error=out.get("text") if out.get("error") else None)

    def run(self, tasks: list[SubagentTask]) -> list[SubagentResult]:
        if not tasks:
            return []
        results: list[Optional[SubagentResult]] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=min(self.max_concurrent, len(tasks))) as pool:
            futs = {pool.submit(self._run_one, t): i for i, t in enumerate(tasks)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    results[i] = SubagentResult(label=tasks[i].label,
                                                role=tasks[i].role, text="",
                                                error=f"{type(e).__name__}: {e}")
        return [r for r in results if r is not None]
