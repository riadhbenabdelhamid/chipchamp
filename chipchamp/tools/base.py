"""Agent tool contract (SPEC §9).

Every tool declares a **cost class** (free/cheap/metered/licensed) and a
**permission tier** (read/write/submit/approve), carries a JSON input schema
(so the model can call it), and returns a JSON-serializable dict with provenance.
The registry here is the platform's API to the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

TOOLS: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    description: str
    cost: str  # free | cheap | metered | licensed
    permission: str  # read | write | submit | approve
    schema: dict
    handler: Callable[..., dict]
    group: str = ""
    destructive: bool = False  # irreversibly destroys data (delete/overwrite);
    #                            ALWAYS human-confirmed, independent of autonomy

    def to_anthropic(self) -> dict:
        return {"name": self.name.replace(".", "__"),
                "description": self.description,
                "input_schema": self.schema}


def tool(name: str, description: str, *, cost: str = "free",
         permission: str = "read", group: str = "", schema: dict | None = None,
         destructive: bool = False):
    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        TOOLS[name] = Tool(name=name, description=description, cost=cost,
                           permission=permission,
                           schema=schema or {"type": "object", "properties": {}},
                           handler=fn, group=group, destructive=destructive)
        return fn
    return deco


def truncate(obj: Any, max_items: int = 60, max_str: int = 4000) -> Any:
    """Enforce per-result ceilings with explicit truncation markers (FR-CTX-01)."""
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + f"\n…[truncated {len(obj)-max_str} chars]"
    if isinstance(obj, list):
        out = [truncate(x, max_items, max_str) for x in obj[:max_items]]
        if len(obj) > max_items:
            out.append(f"…[truncated {len(obj)-max_items} more items]")
        return out
    if isinstance(obj, dict):
        return {k: truncate(v, max_items, max_str) for k, v in obj.items()}
    return obj
