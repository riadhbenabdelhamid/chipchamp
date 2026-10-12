"""Cheap argument suggestions; completion never launches a tool or model."""
from __future__ import annotations

import shlex


def arguments(ctx, text):
    try:
        words = shlex.split(text[1:])
    except ValueError:
        return []
    if not words:
        return []
    if text[-1:].isspace():
        words.append("")
    command, current = words[0], words[-1]
    choices = []
    if command == "theme" and len(words) == 2:
        from ..ui import theme_names
        choices = [(n, "theme") for n in theme_names() + ["off"]]
    elif command == "ui":
        options = ["compact", "verbose", "motion"] if len(words) == 2 else (
            ["on", "off"] if len(words) == 3 and words[1] == "motion" else [])
        choices = [(n, "display preference") for n in options]
    elif command == "mode" and len(words) == 2:
        choices = [(n, "autonomy mode") for n in ("normal", "auto", "plan")]
    elif command == "job" and len(words) == 3 and not current.startswith("-"):
        choices = [(n, "job view") for n in ("summary", "log", "waves", "rerun")]
    elif command in ("job", "repro") and len(words) == 2:
        try:
            choices = [(r.id, f"{r.kind} · {r.status}") for r in reversed(ctx.runner.list_jobs()[-25:])
                       if r.target_name == ctx.target_name]
        except OSError:
            pass
    elif (command in ("sim", "cov") and len(words) == 2 and not current.startswith("-")) or words[-2:-1] == ["--test"]:
        choices = [(t["name"], "test") for t in ctx.ws.tests if t.get("name")]
    else:
        from ._core import cli
        cmd = cli.get_command(None, command)
        if cmd and current.startswith("-"):
            choices = [(o, p.help or "option") for p in cmd.params
                       for o in getattr(p, "opts", []) if o.startswith("--")]
        elif words[-2:-1] == ["--signal"]:
            # Only already-loaded waves: TAB must not parse a multi-GB dump.
            choices = [(p, "signal") for wave in ctx.open_waves.values() for p in wave.data.by_path]
    return [(value, desc, len(current)) for value, desc in choices if value.startswith(current)]
