"""EDA adapter contract (SPEC §8.4).

One normalized interface over heterogeneous tools. Each adapter turns a request
(lint / sim / synth / formal / lec / coverage) into a :class:`Plan` of shell
steps that the job runner executes, then parses raw tool output into
:class:`NormalizedDiagnostic` records and structured metrics. Adapters declare a
:class:`CapabilityManifest` (FR-ADPT-01) and never embed vendor binaries — they
drive the customer's installed tools (SPEC §19 R3/R6).
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NormalizedDiagnostic:
    tool: str
    severity: str  # error | warning | info
    message: str
    file: str = ""
    line: int = 0
    col: int = 0
    code: str = ""
    hier: str = ""
    category: str = ""  # elab | lint | cdc | sim | synth | formal | lec
    waiver: Optional[str] = None

    def key(self) -> str:
        return f"{self.code}|{self.file}:{self.line}|{self.severity}"


@dataclass
class CapabilityManifest:
    name: str
    roles: list[str]
    languages: list[str] = field(default_factory=lambda: ["sv"])
    standards: list[str] = field(default_factory=list)
    coverage_formats: list[str] = field(default_factory=list)
    wave_formats: list[str] = field(default_factory=list)
    xprop: bool = False
    license_features: list[str] = field(default_factory=list)
    known_gaps: list[str] = field(default_factory=list)


@dataclass
class Step:
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    allow_fail: bool = False


@dataclass
class Plan:
    kind: str  # lint | sim | synth | lec | formal | coverage | elab | format
    steps: list[Step]
    workdir: str
    adapter: str
    artifacts: dict[str, str] = field(default_factory=dict)  # logical -> path
    license_features: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class StepResult:
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float = 0.0
    timed_out: bool = False


@dataclass
class ToolResult:
    ok: bool
    kind: str
    adapter: str
    summary: str = ""
    status: str = ""  # sim: pass | fail | error | timeout
    diagnostics: list[NormalizedDiagnostic] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> list[NormalizedDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[NormalizedDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]


class Adapter:
    """Base class. Subclasses implement the role methods they support and a
    single :meth:`parse` that dispatches on ``plan.kind``."""

    name: str = "base"
    binary: str = ""
    cost: str = "cheap"  # free | cheap | metered | licensed

    def available(self) -> bool:
        return bool(self.binary) and shutil.which(self.binary) is not None

    version_flags: tuple[str, ...] = ("--version", "-V", "--help")

    def version(self) -> str:
        if not self.available():
            return "not-installed"
        bad = ("invalid option", "unknown option", "unrecognized", "usage:")
        for flag in self.version_flags:
            try:
                out = subprocess.run([self.binary, flag], capture_output=True,
                                     text=True, timeout=15)
                for line in (out.stdout or out.stderr).strip().splitlines():
                    line = line.strip()
                    if line and not any(b in line.lower() for b in bad):
                        return line
            except (subprocess.SubprocessError, OSError):
                continue
        return "unknown"

    def manifest(self) -> CapabilityManifest:  # pragma: no cover - overridden
        return CapabilityManifest(name=self.name, roles=[])

    def parse(self, plan: Plan, results: list[StepResult]) -> ToolResult:
        raise NotImplementedError

    # helper for adapters that just want an "unavailable" result
    def unavailable_result(self, kind: str) -> ToolResult:
        return ToolResult(
            ok=False, kind=kind, adapter=self.name, status="error",
            summary=f"{self.name} ({self.binary}) is not installed on PATH",
            diagnostics=[NormalizedDiagnostic(
                tool=self.name, severity="error", category="tool",
                code="TOOL-MISSING",
                message=f"{self.binary} not found; install it or select another adapter")])
