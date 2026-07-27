"""Farm connectors (SPEC §8.5 M2): LSF and SLURM command wrapping.

The local JobRunner keeps its synchronous submit-and-parse contract; a farm
config wraps each step's argv in a *blocking* scheduler submission (`bsub -K`,
`srun`) so completion still wakes the caller (FR-JOB-04) and license tokens are
still held for the duration. Resource strings and queues are policy from
config (FR-JOB-02), never agent improvisation.

`dry_run` returns the exact submission argv without executing — that is how the
wrapping is tested on machines without a scheduler (this one), and how a CAD
team can audit what Chipchamp would submit before enabling it.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field


@dataclass
class FarmConfig:
    kind: str = "local"  # local | lsf | slurm
    queue: str = ""      # LSF queue / SLURM partition
    resources: str = ""  # e.g. LSF "rusage[mem=16G]" / SLURM "--mem=16G"
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict) -> "FarmConfig":
        r = config.get("runner", {}) or {}
        return cls(kind=r.get("kind", "local"), queue=r.get("queue", ""),
                   resources=r.get("resources", ""),
                   extra_args=list(r.get("extra_args", [])))


_SUBMIT_BIN = {"lsf": "bsub", "slurm": "srun"}


def scheduler_available(kind: str) -> bool:
    binary = _SUBMIT_BIN.get(kind)
    return bool(binary and shutil.which(binary))


def wrap_for_farm(argv: list[str], farm: FarmConfig, job_name: str = "chipchamp") -> list[str]:
    """Wrap a step command in a blocking scheduler submission."""
    if farm.kind == "lsf":
        cmd = ["bsub", "-K", "-J", job_name]  # -K: block until done (FR-JOB-04)
        if farm.queue:
            cmd += ["-q", farm.queue]
        if farm.resources:
            cmd += ["-R", farm.resources]
        cmd += farm.extra_args
        return cmd + argv
    if farm.kind == "slurm":
        cmd = ["srun", f"--job-name={job_name}"]  # srun blocks by default
        if farm.queue:
            cmd += [f"--partition={farm.queue}"]
        if farm.resources:
            cmd += farm.resources.split()
        cmd += farm.extra_args
        return cmd + argv
    return argv  # local


def plan_submission(argv: list[str], farm: FarmConfig, job_name: str = "chipchamp",
                    dry_run: bool = False) -> tuple[list[str], str]:
    """Return (final_argv, note). Falls back to local execution with an explicit
    note when the scheduler binary is absent — degraded, never silent."""
    if farm.kind == "local":
        return argv, "local"
    if not scheduler_available(farm.kind) and not dry_run:
        return argv, (f"{farm.kind} requested but "
                      f"'{_SUBMIT_BIN[farm.kind]}' not on PATH — ran locally")
    return wrap_for_farm(argv, farm, job_name), farm.kind
