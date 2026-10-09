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


@tool("mutation.run", "Test whether checks detect small bugs on changed RTL lines in isolated "
      "copies. Requires a passing baseline for every selected test/seed; orders tests by "
      "coverage history. Reports survivors, compile errors, timeouts and supporting jobs. "
      "Consumes V3 budget; never supplies completion-gate evidence.", cost="metered",
      permission="submit", group="sim", schema={"type": "object", "properties": {
          "changed_paths": {"type": "array", "items": {"type": "string"}},
          "tests": {"type": "array", "items": {"type": "string"}},
          "seeds": {"type": "array", "items": {"type": "integer"}},
          "base": {"type": "string", "default": "HEAD"},
          "max_mutants": {"type": "integer", "minimum": 1, "maximum": 100, "default": 8},
          "max_jobs": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 20},
          "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 14400, "default": 120},
          "max_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 14400, "default": 600}}})
def mutation_run(ctx, changed_paths=None, tests=None, seeds=None, base="HEAD",
                 max_mutants=8, max_jobs=20, timeout_s=120, max_seconds=600):
    from ..mutation import run_mutations
    try:
        return run_mutations(ctx, changed_paths, tests, seeds, base,
                             max_mutants, max_jobs, timeout_s, max_seconds)
    except PathRefused as e:
        return e.payload
    except (ValueError, OSError) as e:
        return {"error": str(e)}
