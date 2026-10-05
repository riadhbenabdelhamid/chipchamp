"""One way to run a tool, whoever is calling.

The agent loop's `_exec` was the only place that coerced JSON-quoted
scalars to the schema's types, turned a BudgetExceeded into a "retrying
will not help" result, named the accepted parameters on a bad-argument
TypeError, shaped a crash into an error dict, flagged a cache hit as not
fresh work, and scanned the output for instruction-shaped text. The MCP
server and twenty-nine CLI commands called `tool.handler(ctx, **args)`
directly and got none of it — a budget ceiling reached from `chipchamp sim`
was a raw exception, a cached verdict looked like a fresh run, and a log
line saying "ignore all previous instructions" went through unflagged.

`invoke()` is that policy. The loop adds what is the loop's business —
unknown-tool teaching, destructive/plan-mode gating, job start/finish
events — around it.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from ..policy.budgets import BudgetExceeded
from .base import Tool


def coerce_args(args: dict, schema: dict) -> dict:
    """Coerce JSON-quoted scalars to what the tool's schema declares.

    Local models constantly quote scalars — `"time": "825000"`,
    `"waves": "True"` — and a handler comparing that string to an int raises a
    raw TypeError from somewhere deep ('< not supported…'), which the
    bad-argument path then mislabels: the parameter NAMES are right, so the
    `accepts` hint cannot help, and a model retries the same call verbatim.
    The best run of the demo campaign burned its last six steps exactly there.

    Only unambiguous coercions happen: digit-strings to int, numeric strings
    to float, true/false spellings to bool, and integral floats to int where
    the schema says integer. Anything else passes through untouched so the
    tool's own validation still speaks."""
    props = (schema or {}).get("properties") or {}
    out = dict(args or {})
    for k, v in out.items():
        want = (props.get(k) or {}).get("type")
        try:
            if want == "integer":
                if isinstance(v, str) and re.fullmatch(r"[+-]?\d+", v.strip()):
                    out[k] = int(v)
                elif isinstance(v, float) and v.is_integer():
                    out[k] = int(v)
            elif want == "number" and isinstance(v, str):
                out[k] = float(v.strip())
            elif want == "boolean" and isinstance(v, str) \
                    and v.strip().lower() in ("true", "false"):
                out[k] = v.strip().lower() == "true"
        except (ValueError, TypeError):
            continue
    return out


def stringify(result) -> str:
    """A bounded text form of a result, for scanning and transcripts."""
    try:
        return json.dumps(result)[:12000]
    except (TypeError, ValueError):
        return str(result)[:12000]


def invoke(ctx, tool: Tool, args: Optional[dict] = None, *,
           on_event: Optional[Callable[[str, dict], None]] = None,
           name: str = "") -> dict:
    """Run `tool` on `ctx` with `args`, under the one execution policy.

    Always returns a dict: the tool's own result, or a shaped error / denial.
    `on_event` receives "budget_block" and "security" events when given.
    `tool` may be any object with handler/schema (tests use bare namespaces);
    `name` is used in messages when the object carries none."""
    name = name or getattr(tool, "name", "") or "tool"
    schema = getattr(tool, "schema", None) or {}
    emit = on_event or (lambda _k, _d: None)
    before = len(getattr(ctx, "task_jobs", []))
    try:
        result = tool.handler(ctx, **coerce_args(args or {}, schema))
    except BudgetExceeded as e:
        # FR-BUDG-01: a ceiling pauses the task and asks the human. The
        # caller is told plainly that retrying cannot help, so an agent
        # reports what it has instead of grinding against a wall.
        emit("budget_block", {"name": name, "reason": str(e)})
        result = {"denied": True, "budget": True, "tool": name,
                  "note": f"{e}. Retrying will not help — no further jobs "
                          f"of this kind can run. Summarize what the "
                          f"evidence so far supports and stop."}
    except TypeError as e:
        # Name what is accepted, not just what was rejected. A live run had
        # a model pass `wave=True` for `waves` and get back only "got an
        # unexpected keyword" — true, unactionable, and it did not recover.
        ok = sorted((schema.get("properties") or {}))
        result = {"error": f"bad arguments for {name}: {e}",
                  "accepts": ok,
                  "required": list(schema.get("required") or []),
                  "note": "call it again using only the parameter names in "
                          "'accepts'"}
    except KeyboardInterrupt:
        raise
    except Exception as e:  # a tool crash must not kill the caller
        result = {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(result, dict):
        result = {"result": result}
    # A reused verdict is real evidence but not fresh work: say so in the
    # result, so neither a model nor a reader can mistake a cache hit for a
    # tool that just ran.
    jobs = getattr(ctx, "task_jobs", [])
    if any(getattr(r, "cached", False) for r in jobs[before:]):
        result["cached"] = True
    # FR-SEC-05: flag instruction-shaped content inside tool output — it is
    # data from the design/logs, never instructions.
    from ..policy.injection import annotate_result, scan_text
    findings = scan_text(stringify(result))
    if findings:
        result = annotate_result(result, findings)
        emit("security", {"tool": name, "findings": findings})
    return result
