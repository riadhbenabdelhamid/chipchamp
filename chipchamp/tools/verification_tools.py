"""Verification planning shared by CLI, MCP and agent sessions."""
from .base import tool
from .paths import PathRefused


@tool("verify.plan", "Explain changed-module and line-level coverage impact, selected regression tests, "
      "seeds, required gates and V3 budget. Uses session edits or Git changes; "
      "changed_paths scopes an explicit plan. Does not launch jobs or waive gates.",
      cost="cheap", group="plan", schema={"type": "object", "properties": {
          "changed_paths": {"type": "array", "items": {"type": "string"}},
          "seeds": {"type": "array", "items": {"type": "integer"}},
          "base": {"type": "string", "default": "HEAD"}}})
def verify_plan(ctx, changed_paths=None, seeds=None, base="HEAD"):
    from ..verification import verification_plan
    try:
        return verification_plan(ctx, changed_paths, seeds, base)
    except PathRefused as e:
        return e.payload
    except (ValueError, OSError) as e:
        return {"error": str(e)}
