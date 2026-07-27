"""Tool catalog assembly (SPEC §9). Importing this registers every tool."""
from __future__ import annotations

from . import build_tools  # noqa: F401
from . import design_tools  # noqa: F401
from . import efpga_tools  # noqa: F401
from . import fpga_tools  # noqa: F401
from . import fs_tools  # noqa: F401
from . import lib_tools  # noqa: F401
from . import meta_tools  # noqa: F401
from . import pd_tools  # noqa: F401
from . import plan_tools  # noqa: F401
from . import regmap_tools  # noqa: F401
from . import riscv_tools  # noqa: F401
from . import skill_tools  # noqa: F401
from . import uvm_tools  # noqa: F401
from . import verif_tools  # noqa: F401
from . import web_tools  # noqa: F401
from .base import TOOLS, Tool


def _fill_role_enum() -> None:
    """Publish the subagent role names into agent.spawn's schema.

    Deferred to here because agent.roles imports the tool catalog: resolving
    the names inside meta_tools is an import cycle that fails quietly and
    leaves `enum: []`, which is worse than no enum — it forbids every value.
    Idempotent, and a failure leaves the enum key absent rather than empty."""
    spawn = TOOLS.get("agent.spawn")
    if spawn is None:
        return
    try:
        from ..agent.roles import builtin_roles
        names = sorted(builtin_roles())
    except Exception:
        names = []
    role = (spawn.schema.get("properties", {}).get("tasks", {})
            .get("items", {}).get("properties", {}).get("role"))
    if role is None:
        return
    if names:
        role["enum"] = names
    else:
        role.pop("enum", None)   # never ship an empty enum


def all_tools() -> dict[str, Tool]:
    _fill_role_enum()
    return dict(TOOLS)


def tools_for_permissions(allowed: set[str]) -> dict[str, Tool]:
    return {n: t for n, t in TOOLS.items() if t.permission in allowed}


def catalog_summary() -> list[dict]:
    return [{"name": t.name, "group": t.group, "cost": t.cost,
             "permission": t.permission, "description": t.description}
            for t in sorted(TOOLS.values(), key=lambda x: (x.group, x.name))]
