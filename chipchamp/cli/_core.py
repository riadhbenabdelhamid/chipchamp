"""Chipchamp CLI (SPEC §7.1) — the primary surface.

Interactive/agentic and headless. Slash-command-equivalent subcommands each map
to a shipped skill or a direct tool call. Everything runs against a Workspace
built from ``.chipchamp/`` config; nothing here reaches the network except the
optional model-driven ``agent`` command.

The CLI is a package: this module holds the root group, the console and the
helpers every command module uses; the commands live by subject in design,
physical, riscv, verify, model, repl and knowledge.
"""
from __future__ import annotations

import json
import sys

import click

from .. import __version__
from .. import brand as _brand
from ..config import Workspace
from ..tools import all_tools
from ..tools.executor import invoke as _invoke
from ..tools.context import ToolContext

try:
    from rich.table import Table  # noqa: F401
    from .. import ui as _ui
    # ONE console: ui.set_theme() pushes the theme onto ui's console, so a
    # second Console here printed the banner, /help, /effort and model
    # status in the default colours whatever [ui] theme said.
    _con = _ui.console()
except Exception:  # pragma: no cover
    _con = None



def _run_tool(c, name: str, **kw) -> dict:
    """Run a catalog tool from a CLI command under the same execution policy
    as the agent loop (argument coercion, budget stop, injection scan)."""
    return _invoke(c, all_tools()[name], kw)


def _console():
    """The console the CLI prints to. Looked up through the package so a
    test that monkeypatches `chipchamp.cli._con` is honoured everywhere."""
    pkg = sys.modules.get("chipchamp.cli")
    return getattr(pkg, "_con", _con) if pkg is not None else _con


def echo(msg="", **kw):
    """`kw` goes to rich (e.g. ``highlight=False`` for hand-styled graphics,
    whose digits the repr highlighter would otherwise re-color)."""
    con = _console()
    if con:
        con.print(msg, **kw)
    else:
        click.echo(msg if isinstance(msg, str) else str(msg))


def _ctx(root, target=None) -> ToolContext:
    ws = Workspace(root)
    from .. import ui
    from ..ui_preferences import load
    ui.configure(load(ws))
    return ToolContext(ws, target=target)


def _emit(obj, as_json):
    if as_json:
        click.echo(json.dumps(obj, indent=2, default=str))
    else:
        echo(obj)


@click.group(context_settings={"help_option_names": ["-h", "--help"]},
             invoke_without_command=True)
@click.version_option(__version__, prog_name="chipchamp")
@click.option("--root", default=None,
              help="working repo (default: the repo you launched from)")
@click.option("--target", default=None, help="build target")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.option("-p", "--prompt", default=None,
              help="headless one-shot task, then exit (like `claude -p`)")
@click.option("--model", default=None, help="override model id for this session")
@click.option("--provider", default=None,
              help="override provider (anthropic/openai/ollama/lmstudio/…)")
@click.option("--max-steps", default=40, help="max agent steps per task")
@click.option("--plan", "plan_mode", is_flag=True, default=None,
              help="plan mode: approve each edit/job before it runs")
@click.option("--continue", "continue_last", is_flag=True, default=False,
              help="resume the most recent session in this workspace")
@click.option("--resume", "resume_id", default=None,
              help="resume a specific session by id (see /sessions)")
@click.option("--experiment", "experiment", default=None,
              help="record this run as an experiment under the given label "
                   "(see `chipchamp experiment`)")
@click.pass_context
def cli(ctx, root, target, as_json, prompt, model, provider, max_steps,
        plan_mode, continue_last, resume_id, experiment):
    """Chipchamp — an agentic coding platform for RTL design & verification.

    Launch from any repo — that becomes the working repo (auto-detected from the
    git root) unless you set one with --root, CHIPCHAMP_ROOT, or `chipchamp
    workspace <path>`. Run with no command for an interactive session; or run a
    command directly, e.g. `chipchamp sim fifo_smoke`, `chipchamp -p "why fail?"`.
    """
    from ..config import resolve_root
    resolved, how = resolve_root(root)
    ctx.ensure_object(dict)
    ctx.obj.update(root=resolved, root_how=how, target=target, as_json=as_json)
    if ctx.invoked_subcommand is None:
        from .repl import _interactive  # lazy: repl imports this module
        _interactive(ctx.obj, prompt=prompt, model=model, provider=provider,
                     max_steps=max_steps, plan_mode=plan_mode,
                     resume=resume_id or ("__latest__" if continue_last else None),
                     experiment=experiment)


# ---- design intelligence ----------------------------------------------------


def _short(d):
    s = json.dumps(d) if not isinstance(d, str) else d
    return s[:60]


def main():
    # The product name is a variable (brand.APP_NAME), set from the main
    # program: rebrand every surface — banner, markers, env-var names, dot-dir
    # for NEW workspaces — via brand.set_name() here, or CHIPCHAMP_BRAND=<name>
    # without touching code. Old workspaces/harnesses keep working (legacy
    # names stay accepted; see brand.LEGACY_NAMES).
    _brand.set_name(_brand.env("BRAND", ""))
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
