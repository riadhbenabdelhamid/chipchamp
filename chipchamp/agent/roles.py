"""Typed subagent roles with least-privilege tool sets (SPEC §8.10, FR-MA-01).

Every role's tool set excludes ``report.done`` and ``evidence.bundle``: only the
orchestrator may report task success (FR-MA-03) — enforced structurally, because a
subagent literally does not have the tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..brand import marker
from ..tools import all_tools
from ..tools.web_tools import RESEARCHER_SUFFIX

# tool groups that are read-only over the design/artifacts
_READ_GROUPS = {"design", "wave", "cov", "sim"}  # sim group incl. job.log/status (read)
_READ_ONLY_NAMES = {"fs.read", "fs.list", "fs.grep", "repro", "doc.blockdiagram",
                    "policy.check", "sim.list_tests", "test.select",
                    "job.log", "job.status", "regress.failures"}
# submit-capable read tools a triage analyst may use to reproduce
_REPRO_NAMES = {"sim.run"}
# never available to ANY subagent (orchestrator-only, FR-MA-03)
_ORCHESTRATOR_ONLY = {"report.done", "evidence.bundle", "mr.prepare"}


@dataclass
class Role:
    name: str
    description: str
    tool_names: set[str]
    max_steps: int = 15
    system_suffix: str = ""

    def tools(self) -> dict:
        cat = all_tools()
        return {n: t for n, t in cat.items()
                if n in self.tool_names and n not in _ORCHESTRATOR_ONLY}


def _named(*names: str) -> set[str]:
    return set(names)


def _read_only() -> set[str]:
    cat = all_tools()
    out = set(_READ_ONLY_NAMES)
    for n, t in cat.items():
        if t.group in ("design", "wave") or (t.group == "cov" and t.permission == "read"):
            out.add(n)
    return out - _ORCHESTRATOR_ONLY


def builtin_roles() -> dict[str, Role]:
    ro = _read_only()
    return {
        "triage-analyst": Role(
            name="triage-analyst",
            description="Root-causes one failure cluster; read-only + rerun-with-waves.",
            tool_names=ro | _REPRO_NAMES,
            max_steps=15,
            system_suffix=(
                "You are a triage analyst assigned ONE failure cluster. Localize the "
                "root cause: rerun the representative with waves if needed, use "
                "wave.when/wave.compare and design.cone, and finish with a concise "
                "root-cause statement citing file:line and the mechanism. You cannot "
                "edit files or declare the task done — report findings as text.")),
        "wave-analyst": Role(
            name="wave-analyst",
            description="Answers waveform questions on an existing run; strictly read-only.",
            tool_names=ro,
            max_steps=10,
            system_suffix="Answer using wave.* queries on the given run. Read-only."),
        "lint-fixer": Role(
            name="lint-fixer",
            description="Fixes lint violations in an assigned file set; edit + lint + smoke.",
            tool_names=ro | _named("fs.edit", "fs.write", "lint.run", "sim.run",
                                   "style.format"),
            max_steps=20,
            system_suffix=(
                "Fix the assigned lint violations mechanically and safely. Never fix a "
                "violation by deleting functionality. After edits run lint.run and a "
                "smoke sim. Report what you changed; the orchestrator owns gates.")),
        "test-writer": Role(
            name="test-writer",
            description="Writes/extends testbenches for an assigned module or hole.",
            tool_names=ro | _named("fs.edit", "fs.write", "sim.run", "cov.run"),
            max_steps=20,
            system_suffix=(
                "Write or extend a self-checking testbench for the assigned target. "
                f"Use {marker('PASS')}/{marker('FAIL')} markers, run it, iterate until it "
                "passes for the right reason.")),
        "reviewer": Role(
            name="reviewer",
            description="Reviews a change set through one lens (design or verification); read-only.",
            tool_names=ro,
            max_steps=12,
            system_suffix=(
                "Review the described change through your assigned lens. Cite concrete "
                "risks with file:line from design.* queries. You cannot edit anything.")),
        "web-researcher": Role(
            name="web-researcher",
            description="Researches an open question on the internet; search + fetch, read-only.",
            tool_names=ro | _named("web.search", "web.fetch"),
            max_steps=10,
            system_suffix=RESEARCHER_SUFFIX),
    }
