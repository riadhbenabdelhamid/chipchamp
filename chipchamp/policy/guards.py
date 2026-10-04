"""Loop-guard settings — the ``[agent.guards]`` section of config.toml.

The guards themselves live in the agent loop (narration nudge, verification
cadence, progress guard, report.done requirement). They were tuned and
verified in headless campaigns where the harness set them explicitly; this
is the config surface that makes the same loop reachable from the product,
so `chipchamp` behaves as measured rather than as a bare default.

``require_report_done`` is the one knob whose right answer depends on how the
loop is driven: a headless run has nobody to notice a model that trails off
in prose, so it must end on report.done; an interactive turn is read by a
human, who may want a plain answer. Left unset, it follows the mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class GuardConfig:
    require_report_done: bool = False
    max_narration_nudges: int = 3
    verify_every_n_writes: int = 3
    progress_guard_after: int = 2

    @classmethod
    def from_config(cls, config: dict, *, headless: bool = False) -> "GuardConfig":
        sec = (config.get("agent") or {}).get("guards") or {}
        rrd: Optional[bool] = sec.get("require_report_done")
        return cls(
            require_report_done=bool(headless) if rrd is None else bool(rrd),
            max_narration_nudges=max(1, int(sec.get("max_narration_nudges", 3))),
            verify_every_n_writes=max(0, int(sec.get("verify_every_n_writes", 3))),
            progress_guard_after=max(0, int(sec.get("progress_guard_after", 2))),
        )

    def as_kwargs(self) -> dict:
        """Keyword arguments for AgentLoop(...), named exactly as it takes them."""
        return {
            "require_report_done": self.require_report_done,
            "max_narration_nudges": self.max_narration_nudges,
            "verify_every_n_writes": self.verify_every_n_writes,
            "progress_guard_after": self.progress_guard_after,
        }
