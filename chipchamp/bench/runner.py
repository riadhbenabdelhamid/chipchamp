"""Cumulative simulator deadlines for sequential mutation jobs."""
from pathlib import Path
import time

from ..adapters.base import StepResult
from ..jobs.runner import JobRunner
from ..util.jsonio import atomic_write


class MutationRunner(JobRunner):
    def __init__(self, store: Path, parent):
        super().__init__(str(store), parent.registry, cache=False,
                         env_modules=parent.env_modules,
                         env_allowlist=parent.env_allowlist, env_deny=parent.env_deny,
                         license_wait_s=parent.license_wait_s)
        self.licenses = parent.licenses
        self.deadline = float("inf")

    def _run_step(self, st, job_dir, idx, timeout, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            message = "mutation runtime limit reached; step was not launched"
            atomic_write(job_dir / f"step{idx}.log", message)
            return StepResult(st.argv, -1, "", message, timed_out=True,
                              timeout_reason="wall")
        return super()._run_step(st, job_dir, idx, min(timeout, remaining), **kwargs)
