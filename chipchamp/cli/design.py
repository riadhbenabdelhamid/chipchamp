"""chipchamp CLI — design commands (split from the one-file cli.py)."""
from __future__ import annotations


import click


try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _ctx, _emit, _run_tool, cli


@cli.command()
@click.option("--rebuild", is_flag=True)
@click.pass_context
def index(ctx, rebuild):
    """Build/refresh the design database."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    db = c.ws.db(rebuild=rebuild)
    fe = "[green]slang[/]" if db.frontend == "slang" else "[yellow]pragmatic[/] (pip install pyslang for the production front end)"
    echo(f"[green]Indexed[/] {len(db.modules)} modules, {len(db.files)} files "
         f"(tree {db.tree_rev[:12]}) · front end: {fe}")
    if db.warnings:
        echo(f"[yellow]{len(db.warnings)} warning(s):[/] " + "; ".join(db.warnings[:5]))
    echo("tops: " + ", ".join(db.tops()))


@cli.command()
@click.argument("top", default="")
@click.option("--depth", default=3)
@click.pass_context
def hier(ctx, top, depth):
    """Show the elaborated design hierarchy."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "design.hierarchy", top=top, depth=depth)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        return
    _print_hier(r["hierarchy"], 0)


def _print_hier(node, ind):
    p = ", ".join(f"{k}={v}" for k, v in list(node.get("params", {}).items())[:4])
    tag = " [red](unresolved)[/]" if node.get("unresolved") else ""
    echo("  " * ind + f"[cyan]{node['inst']}[/] : [bold]{node['module']}[/]"
         + (f"  [dim]#({p})[/]" if p else "") + tag)
    for c in node.get("children", []):
        _print_hier(c, ind + 1)


@cli.command()
@click.argument("module")
@click.pass_context
def module(ctx, module):
    """Show a module card (ports, params, domains, instances)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "design.module", module=module)
    _emit(r if ctx.obj["as_json"] else r.get("card", r), ctx.obj["as_json"])


@cli.command()
@click.argument("module")
@click.argument("signal")
@click.option("--dir", "direction", default="fanin")
@click.option("--depth", default=3)
@click.pass_context
def cone(ctx, module, signal, direction, depth):
    """Extract a signal's fan-in/out cone as a source slice."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "design.cone", module=module, signal=signal,
                                           direction=direction, depth=depth)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    from .. import ui
    stmts = r.get("statements", [])
    if stmts:
        ui.source("\n".join(stmts), lang="systemverilog",
                  title=f"{module}.{signal} ({direction} cone)", line_numbers=False)
    else:
        echo("[dim](no cone found)[/]")


@cli.command()
@click.argument("module", default="")
@click.pass_context
def cdc(ctx, module):
    """Report clock-domain crossings (CDC-lite)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "cdc.run", module=module)
    _emit(r, ctx.obj["as_json"])


@cli.command()
@click.argument("module")
@click.pass_context
def fsm(ctx, module):
    """Show extracted FSM(s) of a module."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _emit(_run_tool(c, "design.fsm", module=module), ctx.obj["as_json"])


@cli.command()
@click.argument("top", default="")
@click.pass_context
def diagram(ctx, top):
    """ASCII block diagram of a hierarchy scope."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "doc.blockdiagram", top=top)
    echo(r.get("diagram", r))


# ---- build / verify ---------------------------------------------------------

@cli.command()
@click.argument("paths", nargs=-1)
@click.pass_context
def lint(ctx, paths):
    """Run lint on the design (or given paths)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "lint.run", paths=list(paths) or None)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    echo(f"[bold]{r.get('adapter')}[/] {r.get('job')}: "
         f"[red]{r.get('errors',0)} error(s)[/], {r.get('warnings',0)} warning(s)")
    for d in r.get("diagnostics", [])[:15]:
        col = "red" if d["severity"] == "error" else "yellow"
        echo(f"  [{col}]{d['severity']}[/] {d['code']} {d['file']}:{d['line']} {d['message'][:80]}")


@cli.command()
@click.argument("test")
@click.option("--seed", default=1)
@click.option("--no-waves", is_flag=True)
@click.pass_context
def sim(ctx, test, seed, no_waves):
    """Compile + run one test on the inner-loop simulator."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "sim.run", test=test, seed=seed, waves=not no_waves)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    col = "green" if r.get("sim_status") == "pass" else "red"
    echo(f"{r.get('job')}: [{col}]{r.get('sim_status')}[/] — {r.get('summary')}")
    if r.get("waves_job"):
        echo(f"  waves: {r['waves_job']} (query with the agent or wave.* tools)")
