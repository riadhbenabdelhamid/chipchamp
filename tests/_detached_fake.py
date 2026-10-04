"""An importable fake adapter for the detached-job tests: a worker process
must be able to rebuild it by module:QualName, so it lives at module level
in an importable module rather than inside a test function."""
from __future__ import annotations

from chipchamp.adapters.base import Adapter, ToolResult


class SlowOk(Adapter):
    name = "slowok"
    kinds = ("sim",)

    def version(self):
        return "1.0"

    def available(self):
        return True

    def plan(self, **kw):
        raise NotImplementedError

    def parse(self, plan, results):
        return ToolResult(ok=True, kind="sim", adapter=self.name, status="pass",
                          summary="worker slept and passed")
