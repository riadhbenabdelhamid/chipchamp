"""Job records (SPEC §8.5): the reproducible unit of everything slow.

A JobRecord captures enough provenance (tool version, env, command lines, input
hashes, seed, license features) that ``chipchamp repro <id>`` can replay it
against the same tree revision (FR-JOB-01). Records are the ground truth that the
evidence bundle and ``report.done`` validate against — the model cannot fabricate
one (SPEC §11.4).
"""
from __future__ import annotations

import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..util.hashing import hash_manifest


@dataclass
class StepLog:
    argv: list[str]
    rc: int
    duration_s: float
    log_path: str
    timed_out: bool = False


@dataclass
class JobRecord:
    id: str
    kind: str  # lint | sim | synth | lec | formal | coverage | elab
    adapter: str
    tool_version: str = ""
    status: str = "queued"  # queued | running | passed | failed | error | timeout
    host: str = field(default_factory=socket.gethostname)
    submit_ts: float = 0.0
    start_ts: float = 0.0
    end_ts: float = 0.0
    inputs_hash: str = ""
    input_files: list[str] = field(default_factory=list)
    defines: dict = field(default_factory=dict)
    seed: Optional[int] = None
    license_features: list[str] = field(default_factory=list)
    env_modules: list[str] = field(default_factory=list)
    steps: list[StepLog] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    # cost accounting (SPEC §11.3 item 8)
    cpu_seconds: float = 0.0
    license_seconds: float = 0.0
    # denormalized result for quick reads without re-parsing logs
    result: dict = field(default_factory=dict)
    # set when this record was SERVED from the job cache rather than run now.
    # Always false on disk: a cache hit never re-persists, so the stored record
    # stays a faithful account of the run that actually happened.
    cached: bool = field(default=False, compare=False)
    # detached jobs (FR-JOB-04): launched with submit_async, so a reader
    # that finds status='running' knows nobody is blocked on it. `ran_as`
    # is the id of the real run this placeholder collected.
    detached: bool = False
    ran_as: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    def compute_inputs_hash(self) -> None:
        self.inputs_hash = hash_manifest(self.input_files)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        steps = [StepLog(**s) for s in d.pop("steps", [])]
        rec = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        rec.steps = steps
        return rec
