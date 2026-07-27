"""Plan / todo tool (SPEC §8.2, FR-CORE-01).

The agent declares a short checklist up front and keeps it current as it works,
so the human sees the intended steps and progress — the plan lives in the
session's ``task_frame`` and survives resume. This is the visible half; plan mode
(the approval gate before write/submit) is enforced in the agent loop.
"""
from __future__ import annotations

from .base import tool
from .context import ToolContext

_STATUSES = ("pending", "in_progress", "done", "skipped")


def _normalize(steps) -> list[dict]:
    # local models sometimes send the list JSON-encoded as a string —
    # decode it rather than iterating the characters (a real qwen3.5 failure)
    if isinstance(steps, str):
        import json
        try:
            decoded = json.loads(steps)
            steps = decoded if isinstance(decoded, list) else [steps]
        except (ValueError, TypeError):
            steps = [steps]
    if isinstance(steps, dict):
        steps = [steps]
    out = []
    for s in steps or []:
        if isinstance(s, str):
            out.append({"step": s.strip(), "status": "pending"})
        elif isinstance(s, dict):
            st = s.get("status", "pending")
            out.append({"step": str(s.get("step", "")).strip(),
                        "status": st if st in _STATUSES else "pending"})
    return [s for s in out if s["step"]]


@tool("plan.update", "Declare or update your step-by-step plan for the current "
      "task as a checklist the human can see. Call this FIRST for any task with "
      "more than a couple of steps, then call it again to mark steps "
      "in_progress/done as you go. Always send the FULL list.",
      permission="read", group="plan",
      schema={"type": "object", "properties": {
          "steps": {"type": "array", "items": {"type": "object", "properties": {
              "step": {"type": "string"},
              "status": {"type": "string",
                         "enum": list(_STATUSES)}},
              "required": ["step"]}}},
          "required": ["steps"]})
def plan_update(ctx: ToolContext, steps) -> dict:
    ctx.plan = _normalize(steps)
    done = sum(1 for s in ctx.plan if s["status"] == "done")
    return {"plan": ctx.plan, "total": len(ctx.plan), "done": done}


@tool("plan.clear", "Clear the current plan.", permission="read", group="plan",
      schema={"type": "object", "properties": {}})
def plan_clear(ctx: ToolContext) -> dict:
    ctx.plan = []
    return {"plan": [], "total": 0, "done": 0}
