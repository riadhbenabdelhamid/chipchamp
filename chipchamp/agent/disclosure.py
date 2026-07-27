"""Progressive tool disclosure (SPEC §8.9 context strategy).

The catalog is 91 tools whose schemas and descriptions come to ~33 KB — about
8.4k tokens shipped on *every* request, before the task has said a word. On a
32k-context local model that is a quarter of the window spent describing eFPGA
bitstream packing to an agent that was asked to fix a lint error.

So the catalog opens at a **core** set and the rest is fetched by group. This is
not a capability restriction — every tool remains reachable, one call away — it
is a statement about when the description has to be in context.

Two design notes:

- The split is by `group`, which every tool already carries, rather than a
  hand-kept list. A new tool joins its group's disclosure automatically; the
  only thing that ever needs editing is CORE.
- `caps` is core. The operating rules already tell the model to probe for a
  backend before planning around it, and that instruction is worthless if the
  probe itself has to be discovered first.

The core set is chosen to cover the bottom of the ladder — read the design,
plan, edit, lint, smoke-sim, read a log, report — because that is the loop
almost every task starts in, and a task that never climbs higher should never
pay for the rungs above it.
"""
from __future__ import annotations

# groups whose tools are always present. Everything else arrives via tools.load.
CORE_GROUPS = {"workspace", "design", "plan", "meta", "skills"}

# individually-cored tools from otherwise-deferred groups: the bottom of the
# ladder (§ operating rule 3) plus the two ways to read a job without dumping it
CORE_TOOLS = {"lint.run", "sim.run", "sim.list_tests", "job.log", "job.status"}


def core_names(tools: dict) -> set[str]:
    return {n for n, t in tools.items()
            if t.group in CORE_GROUPS or n in CORE_TOOLS}


def groups_of(tools: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for n, t in tools.items():
        out.setdefault(t.group, []).append(n)
    return {g: sorted(ns) for g, ns in sorted(out.items())}


def deferred_summary(tools: dict, loaded: set[str]) -> str:
    """The one-line-per-group menu that goes in the system prompt.

    Names only, no schemas — a name plus its group is enough for the model to
    know a capability exists and ask for it, which is the whole trick.
    """
    lines = []
    for g, names in groups_of(tools).items():
        rest = [n for n in names if n not in loaded]
        if rest:
            lines.append(f"  [{g}] " + ", ".join(rest))
    return "\n".join(lines)


DISCLOSURE_RULE = """\
## Tools not yet loaded

Your tool list starts with the core set. These other tools EXIST but their
schemas are not loaded yet — call `tools.load` with the group name(s) to get
them, then call them normally. Do not guess their arguments before loading.

{menu}

Load a group as soon as you know you need it (e.g. `tools.load(groups=["wave"])`
before waveform work). Loading is cheap and permanent for this task."""
