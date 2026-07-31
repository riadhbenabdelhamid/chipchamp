"""Chipchamp CLI (SPEC §7.1) — the primary surface.

Interactive/agentic and headless. Slash-command-equivalent subcommands each map
to a shipped skill or a direct tool call. Everything runs against a Workspace
built from ``.chipchamp/`` config; nothing here reaches the network except the
optional model-driven ``agent`` command.
"""
from __future__ import annotations

import json
import os
import sys

import click

from . import __version__
from . import brand as _brand
from .config import Workspace
from .tools import all_tools, catalog_summary
from .tools.context import ToolContext

try:
    from rich.console import Console
    from rich.table import Table
    _con = Console()
except Exception:  # pragma: no cover
    _con = None


def echo(msg="", **kw):
    """`kw` goes to rich (e.g. ``highlight=False`` for hand-styled graphics,
    whose digits the repr highlighter would otherwise re-color)."""
    if _con:
        _con.print(msg, **kw)
    else:
        click.echo(msg if isinstance(msg, str) else str(msg))


def _ctx(root, target=None) -> ToolContext:
    ws = Workspace(root)
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
    from .config import resolve_root
    resolved, how = resolve_root(root)
    ctx.ensure_object(dict)
    ctx.obj.update(root=resolved, root_how=how, target=target, as_json=as_json)
    if ctx.invoked_subcommand is None:
        _interactive(ctx.obj, prompt=prompt, model=model, provider=provider,
                     max_steps=max_steps, plan_mode=plan_mode,
                     resume=resume_id or ("__latest__" if continue_last else None),
                     experiment=experiment)


# ---- design intelligence ----------------------------------------------------

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
    r = all_tools()["design.hierarchy"].handler(c, top=top, depth=depth)
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
    r = all_tools()["design.module"].handler(c, module=module)
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
    r = all_tools()["design.cone"].handler(c, module=module, signal=signal,
                                           direction=direction, depth=depth)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    from . import ui
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
    r = all_tools()["cdc.run"].handler(c, module=module)
    _emit(r, ctx.obj["as_json"])


@cli.command()
@click.argument("module")
@click.pass_context
def fsm(ctx, module):
    """Show extracted FSM(s) of a module."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _emit(all_tools()["design.fsm"].handler(c, module=module), ctx.obj["as_json"])


@cli.command()
@click.argument("top", default="")
@click.pass_context
def diagram(ctx, top):
    """ASCII block diagram of a hierarchy scope."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["doc.blockdiagram"].handler(c, top=top)
    echo(r.get("diagram", r))


# ---- build / verify ---------------------------------------------------------

@cli.command()
@click.argument("paths", nargs=-1)
@click.pass_context
def lint(ctx, paths):
    """Run lint on the design (or given paths)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["lint.run"].handler(c, paths=list(paths) or None)
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
    r = all_tools()["sim.run"].handler(c, test=test, seed=seed, waves=not no_waves)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    col = "green" if r.get("sim_status") == "pass" else "red"
    echo(f"{r.get('job')}: [{col}]{r.get('sim_status')}[/] — {r.get('summary')}")
    if r.get("waves_job"):
        echo(f"  waves: {r['waves_job']} (query with the agent or wave.* tools)")


@cli.group("efpga-fabulous")
def efpga():
    """Embedded FPGA via FABulous: fabric generation + user-design bitstreams."""


@efpga.command("fabric")
@click.option("--project", default="")
@click.pass_context
def efpga_fabric_cmd(ctx, project):
    """Generate the eFPGA-FABulous fabric HDL for a FABulous project."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    echo("[dim]generating eFPGA-FABulous fabric (minutes)…[/]")
    r = all_tools()["efpga-fabulous.fabric"].handler(c, project=project)
    _emit(r, ctx.obj["as_json"]) if ctx.obj["as_json"] else echo(
        f"[red]{r['error']}[/]" if "error" in r else
        f"{r['job']}: {r['summary']}")


@efpga.command("bitstream")
@click.argument("design")
@click.option("--project", default="")
@click.option("--with-fabric", is_flag=True, help="regenerate the fabric first")
@click.pass_context
def efpga_bitstream_cmd(ctx, design, project, with_fabric):
    """Map a user RTL design onto the fabric → bitstream (synth→P&R→bit_gen)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    echo(f"[dim]mapping {design} onto the eFPGA-FABulous fabric (minutes)…[/]")
    r = all_tools()["efpga-fabulous.bitstream"].handler(
        c, design=design, project=project, with_fabric=with_fabric)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        raise SystemExit(1)
    col = "green" if r["verdict"] == "ok" else "red"
    echo(f"{r['job']}: [{col}]{r['verdict']}[/] — {r['summary']}")
    echo(f"  routed: {r['routed']} · {r['bitstream_bytes']} bytes")
    if r.get("bitstream"):
        echo(f"  [green]bitstream[/]: {r['bitstream']}")


@cli.command()
@click.argument("module")
@click.option("--clock-period", default=10.0)
@click.option("--clock-port", default="clk")
@click.pass_context
def pnr(ctx, module, clock_period, clock_port):
    """Run RTL-to-GDSII for a module (LibreLane) and report signoff."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    echo(f"[dim]running RTL-to-GDSII for {module} (this takes minutes)…[/]")
    r = all_tools()["pd.run"].handler(c, module=module, clock_period=clock_period,
                                      clock_port=clock_port)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        raise SystemExit(1)
    col = "green" if r["verdict"] == "signoff-clean" else "red"
    echo(f"{r['job']}: [{col}]{r['verdict']}[/]")
    s = r["signoff"]
    echo(f"  timing : setup WNS {s['setup_ws_ns']} ns · hold WHS {s['hold_ws_ns']} ns")
    echo(f"  DRC {s['drc_violations']} · LVS {s['lvs_errors']} · antenna {s['antenna_violations']}")
    echo(f"  area   : {s['cell_area_um2']} µm² cells · {s['utilization']} util · "
         f"die {s['die_area_um2']} µm²")
    if r["failing_checks"]:
        echo(f"  [red]failing: {', '.join(r['failing_checks'])}[/]")
    if r.get("gds"):
        echo(f"  [green]GDSII[/]: {r['gds']}")


@cli.command()
@click.option("--top", default="", help="root module (default: target top)")
@click.option("--set", "cov_set", default="default", help="coverage set for dead-width")
@click.pass_context
def optimize(ctx, top, cov_set):
    """PPA optimization brief (P11): area hotspots by RTL line, dead-width
    findings, strategy-sweep Pareto, power picture — a ranked worklist with
    the LEC-proven loop spelled out per item."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    from .agent.playbooks import run_optimize
    echo("[dim]gathering PPA evidence (area/deadwidth/sweep/power)…[/]")
    r = run_optimize(c, top=top, cov_set=cov_set)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    echo(f"[bold]PPA brief: {r['top']}[/]  "
         f"({r.get('total_area_um2', '?')} µm²)")
    for k, v in (r.get("area_hotspots") or [])[:3]:
        echo(f"  area  {k:22} {v['area_um2']:>9} µm² ({v['cells']} cells)")
    if r.get("sweep"):
        echo(f"  sweep pareto: {', '.join(r['sweep']['pareto'])}")
    if r.get("power"):
        p = r["power"]
        echo(f"  power {p.get('total_w')} W  clock {p.get('clock_share_pct')}%"
             f"  [{p.get('activity')}]"
             + (f"  [yellow]{p['warning'][:40]}…[/]" if p.get("warning") else ""))
    if r["worklist"]:
        echo("[bold]worklist[/]")
        for i, w in enumerate(r["worklist"], 1):
            echo(f"  {i}. ({w['kind']}) {w['target']} — {w['evidence']}")
            echo(f"     [dim]{w['loop']}[/]")
    else:
        echo("  [green]no optimization items found[/]")
    echo(f"[dim]{r['note']}[/]")


@cli.command()
@click.argument("module")
@click.option("--flow", type=click.Choice(["auto", "nextpnr", "vivado"]),
              default="auto", help="default: nextpnr if present, else vivado")
@click.option("--family", default="ice40", help="nextpnr family (ice40/ecp5/…)")
@click.option("--part", default="xc7a35tcpg236-1", help="vivado part")
@click.option("--freq", "freq_mhz", default=100.0, help="target clock (MHz)")
@click.option("--baseline", is_flag=True, help="save this run as the PPA baseline")
@click.option("--bitstream", is_flag=True,
              help="pack a flashable bitstream after routing "
                   "(icepack/ecppack/prjoxide or vivado write_bitstream)")
@click.pass_context
def fpga(ctx, module, flow, family, part, freq_mhz, baseline, bitstream):
    """Implement MODULE for an FPGA with stage checkpoints (open flow or
    Vivado .dcp), then report PPA vs baseline and the worst path in RTL terms."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    echo(f"[dim]implementing {module} ({flow})…[/]")
    r = all_tools()["fpga.run"].handler(c, module=module,
                                        flow="" if flow == "auto" else flow,
                                        family=family, part=part,
                                        freq_mhz=freq_mhz)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        raise SystemExit(1)
    col = "green" if r["verdict"] == "pass" else "red"
    echo(f"{r['job']}: [{col}]{r['summary']}[/]")
    echo(f"  checkpoints: {', '.join(r['stages'])}")
    for k, v in r["checkpoints"].items():
        echo(f"    {k:18} {v}")
    if bitstream:
        b = all_tools()["fpga.bitstream"].handler(c, module=module, job=r["job"])
        if "error" in b:
            echo(f"  [red]bitstream: {b['error']}[/]"
                 + (f"\n    [dim]{b['hint']}[/]" if b.get("hint") else ""))
        else:
            echo(f"  [green]bitstream[/] {b['bitstream']} "
                 f"[dim]({b['bitstream_bytes']} bytes via {b['packer']})[/]")
    ppa = all_tools()["fpga.ppa"].handler(c, module=module, job=r["job"],
                                          save_baseline=baseline)
    if "delta" in ppa:
        regs = ppa.get("regressions", [])
        echo(f"  PPA vs baseline: {'[red]' + ', '.join(regs) + ' regressed[/]' if regs else '[green]no regressions[/]'}")
        for k, v in ppa["delta"].items():
            if isinstance(v, dict) and v.get("delta"):
                arrow = "[green]▲[/]" if v.get("better") else "[red]▼[/]" if v.get("better") is False else " "
                echo(f"    {k:22} {v['from']} → {v['to']}  {arrow}")
    if ppa.get("baseline_saved"):
        echo(f"  baseline saved: {ppa['baseline_saved']}")
    crit = all_tools()["fpga.critical"].handler(c, module=module, job=r["job"], n=1)
    for p in crit.get("paths", [])[:1]:
        if crit["flow"] == "vivado":
            echo(f"  worst path: slack {p['slack_ns']} ns  "
                 f"{p.get('source')} → {p.get('destination')} "
                 f"(logic {p.get('logic_ns')} ns / route {p.get('route_ns')} ns, "
                 f"{p.get('logic_levels')} levels)")
        else:
            echo(f"  worst path: {p['delay_ns']} ns on {p['clock']} "
                 f"({p['segments']} segments)")
            for h in p.get("heaviest_rtl", [])[:2]:
                echo(f"    {h['delay_ns']} ns  {h.get('net') or h.get('cell')}  "
                     f"[dim]{'; '.join(h.get('rtl', []))}[/]")


@cli.command()
@click.argument("module", required=False)
@click.option("--check", "check_file", default="",
              help="validate a constraints file (.xdc/.pcf/.lpf) against MODULE's ports")
@click.option("--board", default="", help="board name (see --boards)")
@click.option("--generate", is_flag=True, help="generate constraints for --board")
@click.option("--out", "out_file", default="", help="write generated constraints here")
@click.option("--map", "mappings", multiple=True, metavar="PORT=SIGNAL",
              help="map a port (or bus base) to a board signal. Repeatable.")
@click.option("--boards", "list_boards", is_flag=True, help="list known boards")
@click.option("--import", "import_file", default="",
              help="import a vendor master constraints file as a board profile")
@click.pass_context
def pins(ctx, module, check_file, board, generate, out_file, mappings,
         list_boards, import_file):
    """Pin constraints for a board: validate or generate.

    Catches the pin problems that otherwise surface after synthesis or at
    bitstream time — typos, unconstrained ports, missing IOSTANDARD, duplicate
    pins — in about a second. Board profiles come from Vivado's installed board
    files; add others with --import.
    """
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    T = all_tools()["fpga.pins"].handler
    if list_boards:
        r = T(c, action="boards")
        if ctx.obj["as_json"]:
            _emit(r, True)
            return
        for b in r["boards"]:
            echo(f"  [bold]{b['name']:18}[/] {b['part']:24} "
                 f"[dim]{b['signals']} signals ({b['source']})[/]")
        echo(f"[dim]{len(r['boards'])} board(s) — {r['note']}[/]")
        return
    if import_file:
        r = T(c, action="import", file=import_file, board=board)
        if "error" in r:
            echo(f"[red]{r['error']}[/]")
            raise SystemExit(1)
        echo(f"[green]imported[/] {r['imported']} "
             f"[dim]({r['signals']} signals → {r['profile']})[/]")
        return
    if not module:
        echo("[yellow]give a MODULE[/] [dim](or --boards / --import)[/]")
        raise SystemExit(1)
    mapping = dict(m.split("=", 1) for m in mappings if "=" in m)
    if generate or board and not check_file:
        r = T(c, action="generate", module=module, board=board, map=mapping,
              file=out_file, write=bool(out_file))
        if ctx.obj["as_json"]:
            _emit(r, True)
            return
        if "error" in r:
            echo(f"[red]{r['error']}[/]"
                 + (f"\n[dim]known: {', '.join(r['known'])}[/]" if r.get("known") else ""))
            raise SystemExit(1)
        if r.get("written"):
            echo(f"[green]wrote[/] {r['written']} [dim]({r['mapped']} mapped)[/]")
        else:
            echo(r["constraints"].rstrip())
        if r["unmapped"]:
            echo(f"[yellow]{len(r['unmapped'])} unmapped[/] "
                 f"[dim]{', '.join(r['unmapped'][:8])} — map with "
                 f"--map port=signal (never guessed: a wrong pin can damage "
                 f"hardware)[/]")
        return
    if not check_file:
        echo("[yellow]give --check <file> or --board <name> --generate[/]")
        raise SystemExit(1)
    r = T(c, action="validate", module=module, file=check_file, board=board)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        raise SystemExit(1)
    head = "[green]✓[/]" if r["ok"] else "[red]✗[/]"
    echo(f"{head} {r['file']} [dim]({r['format']}) — {r['constrained']}/"
         f"{r['ports']} ports constrained[/]")
    for f in r["findings"]:
        col = "red" if f["severity"] == "error" else "yellow"
        echo(f"  [{col}]{f['kind']}[/] {f['message']}")
    echo(f"[dim]{r['note']}[/]")
    if not r["ok"]:
        raise SystemExit(1)


@cli.group("riscv", invoke_without_command=True)
@click.pass_context
def riscv_grp(ctx):
    """RISC-V: build test programs and co-simulate a core against a model."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(riscv_info_cmd)


@riscv_grp.command("info")
@click.pass_context
def riscv_info_cmd(ctx):
    """Show the RISC-V toolchain and reference model on this machine."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["riscv.toolchain"].handler(c)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if not r.get("available"):
        echo(f"[yellow]{r.get('error')}[/]\n[dim]{r.get('hint','')}[/]")
        raise SystemExit(1)
    echo(f"[bold]{r['triple']}[/]  [dim]{r['version']}[/]")
    echo(f"  prefix        {r['prefix']}")
    echo(f"  default ISA   {r['default_march']} / {r['default_mabi']}")
    echo(f"  multilibs     {', '.join(r['multilibs']) or '(single)'}")
    model = r.get("reference_model")
    echo(f"  reference     " + (f"[green]{model}[/] {r.get('reference_exe','')}"
                                if model else "[yellow]none[/]"))
    if r.get("note"):
        echo(f"  [dim]{r['note']}[/]")


@riscv_grp.command("compile")
@click.argument("sources", nargs=-1, required=True)
@click.option("--name", default="", help="output basename")
@click.option("--march", default="rv32i", help="ISA under test (rv32i, rv32imc…)")
@click.option("--mabi", default="", help="default: matches --march")
@click.option("--base", default="0x80000000", help="link/load address")
@click.option("--hex-width", default=32, help="memory word width for the image")
@click.pass_context
def riscv_compile_cmd(ctx, sources, name, march, mabi, base, hex_width):
    """Build SOURCES into an ELF + $readmemh image + disassembly."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["riscv.compile"].handler(
        c, sources=list(sources), name=name, march=march, mabi=mabi,
        base=base, hex_width=hex_width)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        for d in r.get("diagnostics", []):
            echo(f"  [dim]{d}[/]")
        if r.get("hint"):
            echo(f"[dim]{r['hint']}[/]")
        raise SystemExit(1)
    echo(f"[green]built[/] {r['elf']} [dim]({r['march']}/{r['mabi']})[/]")
    echo(f"  memory image  {r.get('memory_image')} "
         f"[dim]{r.get('words')} words @ {r.get('base')}[/]")
    echo(f"  disassembly   {r.get('disassembly_file')}")
    for w in r.get("warnings", [])[:5]:
        echo(f"  [yellow]{w}[/]")


@riscv_grp.command("refrun")
@click.argument("elf")
@click.option("--max-steps", default=20000, help="instruction cap")
@click.option("--region", default="", help="gdbsim memory region ADDRESS,SIZE")
@click.option("--timeout", default=60.0, help="wall-clock bound (seconds)")
@click.pass_context
def riscv_refrun_cmd(ctx, elf, max_steps, region, timeout):
    """Run ELF on the reference model and save its trace."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["riscv.refrun"].handler(c, elf=elf, max_steps=max_steps,
                                            region=region, timeout=timeout)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]"
             + (f"\n[dim]{r['hint']}[/]" if r.get("hint") else ""))
        raise SystemExit(1)
    echo(f"[green]{r['backend']}[/] — {r['steps']} retired instructions "
         f"[dim]{r['first_pc']} → {r['last_pc']}[/]")
    if r.get("note"):
        echo(f"  [dim]{r['note']}[/]")
    if r.get("trace_file"):
        echo(f"  trace  {r['trace_file']}")


@riscv_grp.command("cosim")
@click.argument("core_trace")
@click.option("--elf", default="", help="program to run on the reference model")
@click.option("--reference-trace", default="", help="use this trace instead")
@click.option("--skip", default=0, help="ignore N leading steps (boot stub)")
@click.option("--max-steps", default=20000)
@click.option("--region", default="", help="gdbsim memory region ADDRESS,SIZE")
@click.pass_context
def riscv_cosim_cmd(ctx, core_trace, elf, reference_trace, skip, max_steps, region):
    """Compare a core's trace with the reference and report the first divergence."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["riscv.cosim"].handler(
        c, core_trace=core_trace, elf=elf, reference_trace=reference_trace,
        skip=skip, max_steps=max_steps, region=region)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]"
             + (f"\n[dim]{r['hint']}[/]" if r.get("hint") else ""))
        raise SystemExit(1)
    if r["match"]:
        echo(f"[green]✓ lockstep[/] {r['summary']}")
        if r.get("note"):
            echo(f"  [yellow]{r['note']}[/]")
        return
    d = r["divergence"]
    echo(f"[red]✗ divergence[/] {r['summary']}")
    for cx in d.get("context", [])[-4:]:
        echo(f"    [dim]{cx['pc']}  {cx['disasm']}[/]")
    echo(f"  [red]→ #{d['index']}[/]  core {d['core']['pc']}   "
         f"reference {d['reference']['pc']}  [dim]{d['reference']['disasm']}[/]")
    raise SystemExit(1)


@riscv_grp.command("compliance")
@click.option("--config", default="", help="RISCOF config.ini (DUT + reference plugins)")
@click.option("--arch-test", "arch_test", default="",
              help="riscv-arch-test checkout (suite/env derived from it)")
@click.option("--suite", default="", help="riscv-test-suite dir")
@click.option("--env", "env_dir", default="", help="suite env dir")
@click.option("--testfile", default="", help="restrict to a testlist")
@click.option("--timeout", default=3600.0)
@click.pass_context
def riscv_compliance_cmd(ctx, config, arch_test, suite, env_dir, testfile, timeout):
    """Run riscv-arch-test through RISCOF (the isa_compliant gate's evidence)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["riscv.compliance"].handler(
        c, config=config, arch_test=arch_test, suite=suite, env=env_dir,
        testfile=testfile, timeout=timeout)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]"
             + (f"\n[dim]{r['hint']}[/]" if r.get("hint") else ""))
        for ln in r.get("output", [])[:8]:
            echo(f"  [dim]{ln}[/]")
        raise SystemExit(1)
    col = "green" if r["ok"] else "red"
    echo(f"[{col}]{r['summary']}[/] [dim]({r['suite']})[/]")
    for f in r["failures"][:15]:
        echo(f"  [red]FAILED[/] {f}")
    echo(f"[dim]report {r['report']} · log {r['log']}[/]")
    if not r["ok"]:
        raise SystemExit(1)


@cli.command()
@click.argument("module")
@click.option("--out", "out_dir", default="", help="bench dir (default verif/uvm/<module>)")
@click.option("--no-test", is_flag=True, help="don't register in tests.yaml")
@click.pass_context
def uvm(ctx, module, out_dir, no_test):
    """Scaffold a runnable UVM bench for MODULE (agent+env+test+tb, filelist,
    manifest entry). Runs license-free on Verilator 5 + Accellera uvm-core."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["uvm.scaffold"].handler(c, module=module, out_dir=out_dir,
                                            add_test=not no_test)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        raise SystemExit(1)
    echo(f"[green]UVM bench generated[/] in {r['bench_dir']} "
         f"({len(r['files'])} files)")
    for f in r["files"]:
        echo(f"  {f['path']}")
    echo(f"  manifest: {r['manifest']}")
    args = r["run_with"]["args"]
    echo(f"  run: chipchamp sim {args['test']}   [dim](or sim.run in a session)[/]")
    echo(f"  [dim]{r['note']}[/]")


@cli.command()
@click.argument("top", default="")
@click.pass_context
def synth(ctx, top):
    """Synthesis snapshot (cells/area + inferred-latch warnings)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["synth.run"].handler(c, top=top)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    echo(f"{r.get('job')}: {r.get('summary')}  metrics={r.get('metrics')}")
    for d in r.get("warnings", []):
        echo(f"  [yellow]{d['code']}[/]: {d['message'][:90]}")


@cli.command()
@click.argument("test")
@click.option("--set", "cset", default="default")
@click.pass_context
def cov(ctx, test, cset):
    """Run a coverage sim and summarize."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["cov.run"].handler(c, test=test, set=cset)
    _emit(r, ctx.obj["as_json"])


@cli.command()
@click.option("--tag", default="smoke")
@click.option("--seeds", default="1,2,3")
@click.option("--agents", is_flag=True,
              help="fan out one model-driven triage-analyst subagent per cluster (§8.10)")
@click.pass_context
def triage(ctx, tag, seeds, agents):
    """Run tests, cluster failures, and gather root-cause evidence (P1)."""
    from .agent.playbooks import run_triage, run_triage_fanout
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    seed_list = [int(s) for s in seeds.split(",") if s.strip()]
    if agents:
        gw, reason, _ = _build_gateway(c)
        if gw is None:
            echo(f"[yellow]--agents needs a model:[/] {reason}; falling back to scripted triage")
            r = run_triage(c, tag=tag, seeds=seed_list)
        else:
            # subagents may be routed to a different (cheaper) model (§14)
            sub_gw, _, _ = _build_gateway(c, role="subagent")
            sub_gw = sub_gw or gw
            echo(f"[dim]fan-out model: {sub_gw.ref}[/]")
            r = run_triage_fanout(c, lambda role: sub_gw, tag=tag, seeds=seed_list)
    else:
        r = run_triage(c, tag=tag, seeds=seed_list)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        return
    echo(f"[bold]{r['summary']}[/]")
    for cl in r["clusters"]:
        echo(f"\n[red]● {cl['id']}[/] ×{cl['count']} — {cl['signature'][:90]}")
        rep = cl.get("representative") or {}
        echo(f"  representative: {rep.get('test')} seed={rep.get('seed')} job={rep.get('job')}")
        ev = cl.get("evidence", {})
        if ev.get("first_divergence"):
            d = ev["first_divergence"]
            echo(f"  first divergence: [cyan]{d['signal']}[/] @ {d['time']} "
                 f"(fail={d['value_a']} vs pass={d['value_b']})")
        for line in ev.get("cone", [])[:3]:
            echo(f"    [dim]{line.splitlines()[-1]}[/]")
        for s in ev.get("suspects", [])[:2]:
            echo(f"  suspect: {s['commit']} {s.get('subject','')[:50]}")
        if cl.get("analysis"):
            a = cl["analysis"]
            echo(f"  [bold]analyst ({a['steps']} steps):[/] {a['text'][:400]}")
    for fl in r.get("flaky", []):
        echo(f"[yellow]flaky[/]: {fl['test']} pass={fl['pass_seeds']} fail={fl['fail_seeds']}")


@cli.command("lint-burndown")
@click.pass_context
def lint_burndown(ctx):
    """Snapshot lint, format, re-lint, report the delta (P6)."""
    from .agent.playbooks import run_lint_burndown
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _emit(run_lint_burndown(c), ctx.obj["as_json"])


# ---- jobs / repro / adapters / tools ---------------------------------------

@cli.command()
@click.pass_context
def jobs(ctx):
    """List recorded jobs."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    recs = c.runner.list_jobs()
    if ctx.obj["as_json"]:
        _emit([r.to_dict() for r in recs], True)
        return
    for r in recs[-25:]:
        col = {"passed": "green", "failed": "red", "error": "red"}.get(r.status, "yellow")
        echo(f"{r.id}  [{col}]{r.status:8}[/] {r.kind:9} {r.adapter:10} {r.summary[:50]}")


@cli.command()
@click.argument("job")
@click.pass_context
def repro(ctx, job):
    """Print the reproduction command for a job."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = all_tools()["repro"].handler(c, job=job)
    echo(r.get("repro", r.get("error")))


@cli.command()
@click.pass_context
def adapters(ctx):
    """Show EDA adapter capability manifests and availability."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    mans = c.registry.all_manifests()
    roles = c.registry.role_report()
    if ctx.obj["as_json"]:
        _emit({"adapters": mans, "roles": roles}, True)
        return
    if _con:
        t = Table("adapter", "avail", "roles", "version")
        for m in mans:
            t.add_row(m["adapter"], "✓" if m["available"] else "✗",
                      ",".join(m["roles"]), m["version"][:40])
        _con.print(t)
        rt = Table("role", "selected", "live", "pinned")
        for role, v in roles.items():
            rt.add_row(role, v["selected"] or "—",
                       "✓" if v["available"] else "[red]✗[/]",
                       v["pinned"] or "")
        _con.print(rt)
    else:
        for m in mans:
            echo(f"{m['adapter']:12} {'ok' if m['available'] else '--'} {m['roles']}")
        for role, v in roles.items():
            echo(f"role {role:10} -> {v['selected'] or '-'} "
                 f"{'ok' if v['available'] else 'MISSING'}")


@cli.command()
@click.pass_context
def tools(ctx):
    """List the agent tool catalog."""
    if ctx.obj["as_json"]:
        _emit(catalog_summary(), True)
        return
    group = None
    for t in catalog_summary():
        if t["group"] != group:
            group = t["group"]
            echo(f"\n[bold]{group}[/]")
        echo(f"  {t['name']:22} [dim]{t['cost']}/{t['permission']}[/]  {t['description'][:70]}")


# ---- agent ------------------------------------------------------------------

def _build_gateway(c, model_override=None, provider_override=None, role=None):
    """Resolve provider+model from the registry (selection file > env > config).
    `role` consults [model.routing] (§14) — e.g. subagents on a cheaper model."""
    from .agent import ModelGateway, ModelRegistry, NullGateway
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    audit = str(c.ws.dot / "audit.jsonl")
    if provider_override:
        prov = reg.provider(provider_override)
        model = model_override or (prov.config.default_model if prov else "")
        reason = "ok" if prov else f"unknown provider '{provider_override}'"
    else:
        prov, model, reason = reg.resolve(role=role)
        model = model_override or model
    if prov is None:
        return None, reason, audit
    mcfg = c.ws.config.get("model", {}) or {}
    import os as _os
    # 16K output ceiling by default: enough for a reasoning model to plan AND
    # emit a full RTL module in one turn (4K was too low — it burned the budget
    # on reasoning and truncated), but bounded so a single call can't run away
    # to a huge generation and blow the per-call wall-clock timeout on a slow
    # local model. If a module needs more, the loop's length-stop nudge
    # continues it across turns. Override: [model] max_tokens / CHIPCHAMP_MODEL_MAX_TOKENS.
    from .brand import env as _benv
    max_tokens = int(_benv("MODEL_MAX_TOKENS", 0) or
                     mcfg.get("max_tokens", 0) or 16384)
    # Reasoning effort (`/effort`): explicit choice > env > config, normalized
    # and GATED per model by resolve_effort — the single decision point shared
    # with the router's hot-swap path, so a saved effort is never sent to a
    # model that would reject the parameter.
    from .agent.effort import resolve_effort
    ref = f"{prov.name}:{model}"
    effort = resolve_effort(c.ws.dot, ref, c.ws.config)
    # `/model tune`: per-model generation options threaded onto the gateway the
    # same way effort is, so the wire request carries them from the first call.
    from .agent.model_opts import resolve_opts
    options = resolve_opts(c.ws.dot, ref, c.ws.config)
    # `/timeout`: per-model call budget (per-ref > global > config > provider
    # default) — a slow model gets a bigger budget without touching the fast one.
    from .agent.model_timeout import resolve_timeout
    timeout = resolve_timeout(c.ws.dot, ref, c.ws.config)
    return ModelGateway(prov, model, audit_log=audit, timeout=timeout,
                        max_tokens=max_tokens, reasoning_effort=effort,
                        options=options), "ok", audit


def _build_router(c):
    """A ModelRouter loaded from .chipchamp/router.json (+ capability profiles
    from config). Always returned — a no-op while disabled — so `/router` can
    toggle it live on the running loop. None only if the agent module is broken."""
    try:
        from .agent import ModelRegistry
        from .agent.router import ModelRouter, RouterConfig
        reg = ModelRegistry(c.ws.config, str(c.ws.dot))
        return ModelRouter(reg, RouterConfig.load(c.ws.dot), c.ws.config)
    except Exception:
        return None


def _router_status(c, r) -> None:
    from .agent.model_profiles import CAPABILITIES
    cfg = r.cfg
    echo(f"[bold]router[/] " + ("[green]on[/]" if cfg.enabled else "[yellow]off[/]")
         + "   layers: " + " ".join(f"{k}{'✓' if v else '✗'}" for k, v in cfg.layers.items())
         + "   triggers: " + " ".join(f"{k}{'✓' if v else '✗'}" for k, v in cfg.triggers.items()))
    if cfg.pool:
        echo("  pool (ranked strengths):")
        for ref in cfg.pool:
            p = r._profile(ref)
            best = max([c2 for c2 in CAPABILITIES if c2 != "speed"],
                       key=lambda cap: p.score(cap))
            warn = f" · [red]⚠ {','.join(p.failure_modes)}[/]" if p.failure_modes else ""
            echo(f"    [bold]{ref}[/]  [dim]best: {best} {p.score(best):.2f}"
                 f" · speed {p.score('speed'):.2f}[/]{warn}")
    else:
        echo("  pool: [dim](empty) — `/router pool add <provider:model>`[/]")
    for cap, ch in (cfg.chains or {}).items():
        echo(f"  [dim]chain {cap}:[/] {' → '.join(ch)}")


_EFFORT_BLURB = {
    "minimal": "barely thinks · fastest",
    "low": "shallow search · fastest turnaround",
    "medium": "balanced",
    "high": "deep search · longest runtime",
}


def _effort_wave(idx: int, n: int, width: int = 22) -> str:
    """A timing-diagram pulse whose HIGH window is the model's think time.

    Effort is the reasoning analogue of a synthesis effort level: a wider think
    window buys a better result and costs runtime. Rendered as a waveform so the
    trade reads at a glance (`___┌─────┐____`)."""
    hi = max(1, round((idx + 1) / n * (width - 5)))
    return "__┌" + "─" * hi + "┐" + "_" * max(0, width - 4 - hi)


def _effort_status(ref: str, levels, cur: str, sent: str) -> None:
    """Render the lever. `cur` is the persisted choice; `sent` is what
    resolve_effort actually puts on the wire for this model — the display must
    reflect the wire, not the file."""
    echo(f"[bold]⚙ reasoning effort[/] [dim]· {ref or '(no model selected)'}[/]")
    if not levels:
        echo("  [yellow]this model exposes no reasoning-effort control[/]"
             " [dim](not a reasoning model)[/]")
        echo("  [dim]built-in: gpt-oss · gpt-5 · o-series — extend via config "
             "\\[model.effort_support][/]")
        if cur == "off":
            echo("  [dim]effort pinned [bold]off[/] (/effort auto to unpin)[/]")
        elif cur and sent == cur:
            echo(f"  [green]saved effort [bold]{cur}[/] was forced on this "
                 f"model and IS sent[/]")
        elif cur:
            echo(f"  [dim]a saved effort ([bold]{cur}[/]) is set but not sent "
                 f"to this model (it was set on a different one)[/]")
        return
    echo("  [dim]think window · wider = deeper search, longer runtime[/]")
    for i, lv in enumerate(levels):
        active = (lv == sent)
        mark = "[green]▸[/]" if active else " "
        name = f"[bold green]{lv:<8}[/]" if active else f"[dim]{lv:<8}[/]"
        wave = _effort_wave(i, len(levels))
        wave = f"[green]{wave}[/]" if active else f"[grey37]{wave}[/]"
        tail = f"[dim]{_EFFORT_BLURB.get(lv, '')}[/]"
        echo(f"  {mark} {name} {wave}  {tail}"
             + ("  [green]← active[/]" if active else ""))
    if cur == "off":
        echo("  [dim]pinned [bold]off[/] — no field sent (/effort auto to unpin)[/]")
    elif not sent:
        echo("  [dim]unset — the model's own default is used (no field sent)[/]")
    elif not cur:
        echo(f"  [dim]{sent} comes from env/config "
             f"({_brand.env_name('MODEL_REASONING_EFFORT')} / [model] reasoning_effort)[/]")
    echo(f"  [dim]/effort <{('|'.join(levels))}> · /effort off · /effort auto[/]")


def _effort_cmd(c, args, loop=None) -> None:
    """Shared handler for `/effort`: show or set the reasoning-effort lever.

    Persists to .chipchamp/effort.json and retunes the live gateway so the next
    model call uses it (no restart)."""
    from .agent.effort import (LEVELS, load_effort, resolve_effort, save_effort,
                               supported_levels)
    ref = getattr(getattr(loop, "gateway", None), "ref", "") or ""
    if not ref:
        try:
            from .agent import ModelRegistry
            sel = ModelRegistry(c.ws.config, str(c.ws.dot)).current() or {}
            if sel.get("provider"):
                ref = f"{sel['provider']}:{sel.get('model', '')}"
        except Exception:
            ref = ""
    levels = supported_levels(ref, c.ws.config)
    cur = load_effort(c.ws.dot)
    if not args:
        _effort_status(ref, levels, cur, resolve_effort(c.ws.dot, ref, c.ws.config))
        return
    a = args[0].strip().lower()
    if a in ("off", "auto", "default", "clear", "none"):
        # "off" pins effort off (persisted, masks env/config defaults); the
        # other synonyms reset to unset, letting env/config apply again.
        pin = a == "off"
        save_effort(c.ws.dot, "off" if pin else "")
        if loop is not None and getattr(loop, "gateway", None) is not None:
            loop.gateway.reasoning_effort = ""
        if pin:
            echo("[green]effort off — pinned[/] [dim](no field sent, masks any "
                 "env/config default; /effort auto to unpin)[/]")
        else:
            echo("[green]effort reset[/] [dim]— env/config default applies if "
                 "set, else the model's own default[/]")
        return
    valid = levels or LEVELS
    if a not in valid:
        echo(f"[yellow]unknown level '{a}'[/] [dim]— try: {' · '.join(valid)}[/]")
        return
    if levels is None:
        echo(f"[yellow]⚠ {ref or 'this model'} is not known to support "
             f"reasoning_effort[/] [dim]— forcing it for THIS model only; it "
             f"may be ignored or rejected. Declare support via config "
             f"\\[model.effort_support] to make it first-class.[/]")
    save_effort(c.ws.dot, a, ref=ref)
    if loop is not None and getattr(loop, "gateway", None) is not None:
        loop.gateway.reasoning_effort = a
    echo(f"[green]effort → {a}[/] [dim](saved to .chipchamp/effort.json)[/]")
    _effort_status(ref, levels, a, resolve_effort(c.ws.dot, ref, c.ws.config))


def _current_ref(c, loop=None) -> str:
    """The model ref `/model tune` should target: the live gateway's model when
    one is running, else the selection the next gateway would resolve to."""
    gw = getattr(loop, "gateway", None) if loop is not None else None
    ref = getattr(gw, "ref", "") if gw is not None else ""
    if ref and not ref.startswith("offline"):
        return ref
    try:
        from .agent import ModelRegistry
        reg = ModelRegistry(c.ws.config, str(c.ws.dot))
        prov, model, _ = reg.resolve()
        return f"{prov.name}:{model}" if prov else ""
    except Exception:
        return ""


# Reference of generation parameters commonly accepted by local servers, shown
# by `/model tune --help` (and the `chipchamp model --help` epilog). chipchamp
# sends whatever you set VERBATIM, so each server's own docs are authoritative —
# this is the practical shortlist. (name, default, description).
_TUNE_GROUPS = [
    ("sampling — Ollama + LM Studio", [
        ("temperature", "0.8", "randomness; lower = more deterministic"),
        ("top_p", "0.9", "nucleus sampling — keep tokens summing to p"),
        ("top_k", "40", "sample only from the K most-likely tokens"),
        ("min_p", "0.0", "drop tokens below this fraction of the top token"),
        ("repeat_penalty", "1.1", "penalize repetition (Ollama · LM Studio)"),
        ("presence_penalty", "0.0", "OpenAI-style: penalize already-seen tokens"),
        ("frequency_penalty", "0.0", "OpenAI-style: penalize frequent tokens"),
        ("seed", "0", "fix for reproducible output"),
        ("stop", '["</s>"]', "stop sequence(s) — JSON list or string"),
        ("max_tokens", "—", "cap tokens generated this call"),
    ]),
    ("Ollama — context & runtime", [
        ("num_ctx", "4096", "context-window / KV-cache size, in tokens"),
        ("num_predict", "-1", "max tokens to generate (-1 = until stop/full)"),
        ("num_keep", "0", "prompt tokens kept when the context overflows"),
        ("repeat_last_n", "64", "how far back repeat_penalty looks"),
        ("mirostat", "0", "0 off · 1 Mirostat · 2 Mirostat 2.0"),
        ("mirostat_tau", "5.0", "Mirostat target entropy"),
        ("mirostat_eta", "0.1", "Mirostat learning rate"),
        ("num_gpu", "—", "layers to offload to the GPU (0 = CPU only)"),
        ("num_thread", "—", "CPU threads to use"),
        ("num_batch", "512", "prompt batch size"),
    ]),
    ("LM Studio — extras", [
        ("response_format", '{"type":"json_object"}', "constrain output (JSON / schema)"),
        ("logit_bias", '{"123":-100}', "per-token-id bias map"),
        ("ttl", "—", "seconds to keep the model loaded (JIT auto-unload)"),
    ]),
]


def _model_tune_help() -> None:
    """`/model tune --help`: the tunable-parameter reference for local servers."""
    echo("[bold]🎛 /model tune[/] — set generation parameters, sent "
         "[bold]verbatim[/] to your model server")
    echo("  [dim]/model tune <key>=<value> …  ·  --global applies to every model  "
         "·  <key> alone (or key=) clears it  ·  /model tune reset[/]")
    echo("  [dim]values are JSON-typed: 8192→int · 0.7→float · true→bool · "
         r'["</s>"]→list[/]')
    for title, params in _TUNE_GROUPS:
        echo(f"[bold]{title}[/]")
        for name, default, desc in params:
            d = f" [dim](default {default})[/]" if default not in ("—", "") else ""
            echo(f"  [green]{name:<17}[/] {desc}{d}")
    echo("[dim]Commonly-supported keys — your server's docs are the authoritative "
         "list. Unknown keys are sent as-is; a server may ignore or reject one.[/]")


def _tune_epilog() -> str:
    """Plain-text version of the reference for the `chipchamp model` help screen.
    Each paragraph starts with \\b so Click preserves its layout (a single \\b
    would protect only the first paragraph, letting the rest re-wrap)."""
    paras = ["\b\nTunable parameters (--tune KEY=VALUE) — sent verbatim to the "
             "server:"]
    for title, params in _TUNE_GROUPS:
        rows = [f"{title}:"]
        for name, default, desc in params:
            d = f"  (default {default})" if default not in ("—", "") else ""
            rows.append(f"  {name:<17} {desc}{d}")
        paras.append("\b\n" + "\n".join(rows))
    paras.append("Values are JSON-typed. Your server's docs are the authoritative "
                 "list; unknown keys are sent as-is.")
    return "\n\n".join(paras)


def _model_tune_status(c, ref: str) -> None:
    from .agent.model_opts import GLOBAL, load_state, resolve_opts
    eff = resolve_opts(c.ws.dot, ref, c.ws.config)
    st = load_state(c.ws.dot)
    echo(f"[bold]🎛 model tuning[/] [dim]· {ref or '(no model selected)'}[/]")
    if not eff:
        echo("  [dim]no options set — the model runs on provider defaults[/]")
    else:
        for k, v in sorted(eff.items()):
            echo(f"  [green]{k}[/] = {json.dumps(v)}")
    glob = st.get(GLOBAL, {}) or {}
    if glob and ref not in ("", GLOBAL):
        echo("  [dim]incl. global (*): "
             + ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(glob.items()))
             + "[/]")
    echo(r"  [dim]/model tune <key>=<value> … · /model tune \[--global] … · "
         r"/model tune <key> (clear one) · /model tune reset[/]")
    echo("  [dim]sent verbatim in the chat request — [bold]/model tune --help[/] "
         "lists the Ollama / LM Studio parameters you can set[/]")


def _model_tune_cmd(c, args, loop=None) -> None:
    """Shared handler for `/model tune …` (live) and `chipchamp model --tune`
    (persisted to .chipchamp/model_opts.json)."""
    from .agent.model_opts import parse_kv, reset, resolve_opts, set_opts
    if args and args[0] in ("--help", "-h", "help", "?"):
        _model_tune_help()
        return
    glob = False
    if args and args[0] in ("--global", "-g", "global"):
        glob, args = True, args[1:]
    ref = _current_ref(c, loop)
    if not args:
        _model_tune_status(c, ref)
        return
    if args[0] in ("reset", "clear"):
        reset(c.ws.dot, ref, glob=glob)
        echo(f"[green]tuning reset[/] [dim]({'global (*)' if glob else ref})[/]")
    else:
        if not glob and not ref:
            echo("[yellow]no model selected[/] — pick one with [bold]/model[/], "
                 "or tune every model with [bold]/model tune --global …[/]")
            return
        setk, unset = parse_kv(args)
        set_opts(c.ws.dot, ref, setk, unset, glob=glob)
        where = "global (*)" if glob else ref
        if setk:
            echo("[green]tuned[/] "
                 + ", ".join(f"{k}={json.dumps(v)}" for k, v in setk.items())
                 + f" [dim]({where})[/]")
        if unset:
            echo(f"[green]cleared[/] {', '.join(unset)} [dim]({where})[/]")
    # retune the live gateway so the change takes effect on the next call
    if loop is not None and getattr(loop, "gateway", None) is not None:
        try:
            loop.gateway.options = resolve_opts(c.ws.dot, loop.gateway.ref,
                                                c.ws.config)
        except Exception:
            pass
    _model_tune_status(c, ref)


def _fmt_secs(s) -> str:
    if s is None:
        return "provider default"
    if s <= 0:
        return "off (no wall-clock limit)"
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)}m ({int(s)}s)"
    return f"{int(s)}s"


def _parse_duration(tok: str):
    """'1200'|'20m'|'300s' → float seconds; 'off'/'none'/'0' → 0.0; else None."""
    import re as _re
    t = (tok or "").strip().lower()
    if t in ("off", "none", "0"):
        return 0.0
    m = _re.match(r"^(\d+(?:\.\d+)?)(s|m|)$", t)
    if not m:
        return None
    v = float(m.group(1))
    return v * 60 if m.group(2) == "m" else v


def _retune_gateway_timeout(loop, seconds) -> None:
    """Apply a resolved budget to the live gateway + its provider socket. None
    means fall back to the provider default."""
    gw = getattr(loop, "gateway", None) if loop is not None else None
    if gw is None:
        return
    try:
        from .agent.providers.base import DEFAULT_REQUEST_TIMEOUT
        val = DEFAULT_REQUEST_TIMEOUT if seconds is None else float(seconds)
        gw.timeout = val
        if getattr(gw, "provider", None) is not None:
            gw.provider.request_timeout = val
    except Exception:
        pass


def _timeout_status(c, ref: str, loop=None) -> None:
    from .agent.model_timeout import GLOBAL, load_state, resolve_timeout
    eff = resolve_timeout(c.ws.dot, ref, c.ws.config)
    st = load_state(c.ws.dot)
    echo(f"[bold]⏱ model timeout[/] [dim]· {ref or '(no model selected)'}[/]")
    echo(f"  per-call budget: [green]{_fmt_secs(eff)}[/]")
    if ref and ref in st:
        src = "this model"
    elif GLOBAL in st:
        src = "global (*)"
    elif ((c.ws.config.get("model", {}) or {}).get("request_timeout")) is not None:
        src = "[model] request_timeout"
    else:
        src = f"default ({_brand.env_name('MODEL_TIMEOUT')} or 600s)"
    echo(f"  [dim]source: {src}[/]")
    echo(r"  [dim]/timeout <seconds|Nm|off> · /timeout \[--global] <n> · "
         r"/timeout reset[/]")
    echo("  [dim]while streaming this is the max GAP between tokens, not the whole "
         "turn — a model that keeps generating never times out[/]")


def _timeout_cmd(c, args, loop=None) -> None:
    """Shared handler for `/timeout …` (live) and `chipchamp model --timeout`
    (per-model, persisted to .chipchamp/timeout.json)."""
    from .agent.model_timeout import reset, resolve_timeout, set_timeout
    if args and args[0] in ("--help", "-h", "help", "?"):
        echo("[bold]⏱ /timeout[/] — per-model call budget (wall-clock seconds)")
        echo("  [dim]/timeout 1200 · /timeout 20m · /timeout off · /timeout reset "
             r"· /timeout \[--global] 900[/]")
        echo("  [dim]raise it for a slow model without touching the fast ones. "
             "While streaming it bounds the GAP between tokens, so a steadily-"
             "generating model runs as long as it needs.[/]")
        return
    glob = False
    if args and args[0] in ("--global", "-g", "global"):
        glob, args = True, args[1:]
    ref = _current_ref(c, loop)
    if not args or args[0] in ("show", "status"):
        _timeout_status(c, ref, loop)
        return
    if args[0] in ("reset", "auto", "default", "clear"):
        reset(c.ws.dot, ref, glob=glob)
        echo(f"[green]timeout reset[/] [dim]({'global (*)' if glob else ref})[/]")
    else:
        secs = _parse_duration(args[0])
        if secs is None:
            echo(f"[yellow]not a duration:[/] {args[0]} [dim]— give seconds "
                 "(1200), minutes (20m), or off[/]")
            return
        if not glob and not ref:
            echo("[yellow]no model selected[/] — pick one with [bold]/model[/], "
                 "or set a default with [bold]/timeout --global <n>[/]")
            return
        set_timeout(c.ws.dot, ref, secs, glob=glob)
        echo(f"[green]timeout → {_fmt_secs(secs)}[/] "
             f"[dim]({'global (*)' if glob else ref})[/]")
    if loop is not None and getattr(loop, "gateway", None) is not None:
        _retune_gateway_timeout(
            loop, resolve_timeout(c.ws.dot, loop.gateway.ref, c.ws.config))
    _timeout_status(c, ref, loop)


def _offer_timeout_retry(c, loop, line, out) -> str | None:
    """After a model-call timeout, offer a bigger-budget retry (interactive
    only). Returns 'retry' when the caller should re-run the same input."""
    if not sys.stdin.isatty():
        return None  # headless/piped: the error already surfaced; don't block
    ref = out.get("ref") or (getattr(loop, "gateway", None) and loop.gateway.ref) or ""
    cur = out.get("timeout") or 600.0
    nxt = int(cur * 2)
    echo(f"[yellow]⏱ {ref} timed out after {_fmt_secs(cur)}.[/]")
    echo(rf"  [bold]\[r][/] retry with {_fmt_secs(nxt)}   "
         rf"[bold]\[m][/] switch model   [bold]\[enter][/] give up   "
         r"[dim]· /router on auto-switches next time[/]")
    try:
        ans = input("  » ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        echo("")
        return None
    if ans == "m":
        _select_model(c)
        gw2, _, _ = _build_gateway(c)
        if gw2:
            loop.gateway = gw2
            echo(f"[green]switched to {gw2.ref}[/] — resend your request")
        return None
    if ans != "r":
        return None
    from .agent.model_timeout import set_timeout
    if ref:
        set_timeout(c.ws.dot, ref, nxt)  # remember this model is slow
    _retune_gateway_timeout(loop, nxt)
    echo(f"[dim]retrying with {_fmt_secs(nxt)}"
         + (f" (saved for {ref})" if ref else "") + "…[/]")
    return "retry"


def _fetch_server_config(pc, model: str) -> dict:
    """Query a local server for a model's own settings. Ollama exposes the
    Modelfile PARAMETERs via /api/show; LM Studio exposes model info via its
    native /api/v0. Best-effort — returns {'error': …} when unreachable."""
    import json as _json
    import urllib.parse as _up
    import urllib.request as _rq
    base = (getattr(pc, "base_url", "") or "").rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base  # native APIs
    name = (getattr(pc, "name", "") or "").lower()
    hdr = {"Content-Type": "application/json"}
    try:
        if "11434" in base or name == "ollama":
            req = _rq.Request(root + "/api/show",
                              data=_json.dumps({"model": model}).encode(),
                              headers=hdr, method="POST")
            with _rq.urlopen(req, timeout=6) as r:
                data = _json.load(r)
            settings: dict = {}
            for ln in (data.get("parameters") or "").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                k, _, v = ln.partition(" ")
                settings.setdefault(k, []).append(v.strip())     # `stop` repeats
            flat = {k: (v[0] if len(v) == 1 else v) for k, v in settings.items()}
            det = data.get("details") or {}
            for k in ("family", "parameter_size", "quantization_level"):
                if det.get(k):
                    flat[k] = det[k]
            mi = data.get("model_info") or {}
            ctx = next((mi[k] for k in mi if k.endswith("context_length")), None)
            if ctx:
                flat.setdefault("context_length (max)", ctx)
            return {"source": "ollama", "settings": flat}
        if "1234" in base or name in ("lmstudio", "lm-studio", "lm_studio"):
            req = _rq.Request(root + "/api/v0/models/" + _up.quote(model),
                              headers=hdr, method="GET")
            with _rq.urlopen(req, timeout=6) as r:
                data = _json.load(r)
            keys = ("type", "arch", "quantization", "state", "max_context_length",
                    "loaded_context_length", "publisher", "compatibility_type")
            flat = {k: data[k] for k in keys if data.get(k) not in (None, "")}
            return {"source": "lm studio", "settings": flat}
        return {"error": f"don't know how to query '{name or base}' "
                         "(supported: ollama, lm studio)"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _show_model_config(c) -> None:
    """`model --showconfig`: the selected model's settings as the server reports
    them, plus the overrides chipchamp layers on top (effort / tune / timeout)."""
    from .agent import ModelRegistry
    from .agent.effort import resolve_effort
    from .agent.model_opts import resolve_opts
    from .agent.model_timeout import resolve_timeout
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    prov, model, reason = reg.resolve()
    if prov is None or not model:
        echo(f"[yellow]no model selected[/] — pick one with `chipchamp model` "
             f"[dim]({reason})[/]")
        return
    ref = f"{prov.name}:{model}"
    echo(f"[bold]⚙ {ref}[/] [dim]{prov.config.base_url}[/]")
    cfg = _fetch_server_config(prov.config, model)
    if cfg.get("error"):
        echo(f"  [yellow]server settings unavailable:[/] {cfg['error']}")
    else:
        echo(f"  [bold]as configured in {cfg['source']}[/]:")
        if cfg["settings"]:
            for k, v in cfg["settings"].items():
                echo(f"    [green]{k:22}[/] {v}")
        else:
            echo("    [dim](the server exposes no per-parameter defaults)[/]")
    eff = resolve_effort(c.ws.dot, ref, c.ws.config)
    opts = resolve_opts(c.ws.dot, ref, c.ws.config)
    to = resolve_timeout(c.ws.dot, ref, c.ws.config)
    echo("  [bold]chipchamp overrides[/] [dim](sent on top of the above)[/]:")
    echo(f"    [green]{'effort':22}[/] {eff or '(none)'}")
    echo(f"    [green]{'timeout':22}[/] {_fmt_secs(to)}")
    if opts:
        for k, v in sorted(opts.items()):
            echo(f"    [green]{('tune ' + k):22}[/] {json.dumps(v)}")
    else:
        echo(f"    [green]{'tune':22}[/] [dim](none) — /model tune to set[/]")


def _router_cmd(c, args, loop=None) -> None:
    """Shared handler for `/router` (live on the running loop) and
    `chipchamp router …` (transient, persisted to .chipchamp/router.json)."""
    from .agent import ModelRegistry
    from .agent.model_profiles import CAPABILITIES, load_profiles
    from .agent.router import (LAYERS, TRIGGERS, ModelRouter, RouterConfig,
                               telemetry_stats)
    r = getattr(loop, "router", None) if loop is not None else None
    if r is None:
        r = ModelRouter(ModelRegistry(c.ws.config, str(c.ws.dot)),
                        RouterConfig.load(c.ws.dot), c.ws.config)
        if loop is not None:
            loop.router = r
    cfg = r.cfg

    def save():
        cfg.save(c.ws.dot)

    sub = (args[0] if args else "status").lower()
    rest = args[1:]
    if sub in ("status", "st", ""):
        return _router_status(c, r)
    if sub in ("on", "off"):
        cfg.enabled = sub == "on"
        save()
        echo(f"[green]router {sub}[/]")
        return _router_status(c, r) if sub == "on" else None
    if sub == "layer":
        if len(rest) == 2 and rest[0] in LAYERS and rest[1] in ("on", "off"):
            cfg.layers[rest[0]] = rest[1] == "on"
            save()
            return echo(f"[green]layer {rest[0]} → {rest[1]}[/]")
        return echo(f"[yellow]usage: /router layer <{'|'.join(LAYERS)}> on|off[/]")
    if sub in ("trigger", "triggers"):
        if len(rest) == 2 and rest[0] in TRIGGERS and rest[1] in ("on", "off"):
            cfg.triggers[rest[0]] = rest[1] == "on"
            save()
            return echo(f"[green]trigger {rest[0]} → {rest[1]}[/]")
        return echo(f"[yellow]usage: /router triggers <{'|'.join(TRIGGERS)}> on|off[/]")
    if sub == "pool":
        act = rest[0] if rest else "ls"
        if act == "add" and len(rest) >= 2:
            for ref in rest[1:]:
                if ref not in cfg.pool:
                    cfg.pool.append(ref)
            save()
        elif act in ("rm", "remove") and len(rest) >= 2:
            cfg.pool = [x for x in cfg.pool if x not in rest[1:]]
            save()
        elif act != "ls":
            return echo("[yellow]usage: /router pool add|rm <provider:model …> | pool ls[/]")
        return echo("  pool: " + (", ".join(cfg.pool) or "[dim](empty)[/]"))
    if sub == "profile":
        if not rest:
            return echo("[yellow]usage: /router profile <ref> [cap=score …][/]")
        ref = rest[0]
        prof = r._profile(ref)
        if len(rest) == 1:
            echo(f"[bold]{ref}[/] [dim]{prof.note}[/]")
            for cap in CAPABILITIES:
                echo(f"  {cap}: {prof.score(cap):.2f}")
            if prof.failure_modes:
                echo(f"  failure_modes: {', '.join(prof.failure_modes)}")
            return
        ov = cfg.profile_overrides.setdefault(ref, {})
        scores = ov.setdefault("scores", dict(prof.scores))
        for kv in rest[1:]:
            k, _, v = kv.partition("=")
            if k in CAPABILITIES:
                try:
                    scores[k] = max(0.0, min(1.0, float(v)))
                except ValueError:
                    pass
        save()
        r.profiles = load_profiles(c.ws.config, cfg.profile_overrides)  # live refresh
        return echo(f"[green]updated profile {ref}[/]")
    if sub == "chain":
        if len(rest) >= 2 and rest[0] in CAPABILITIES:
            cfg.chains[rest[0]] = [x for x in
                                   " ".join(rest[1:]).replace(",", " ").split() if x]
            save()
            return echo(f"[green]chain {rest[0]}: {' → '.join(cfg.chains[rest[0]])}[/]")
        return echo(f"[yellow]usage: /router chain <{'|'.join(CAPABILITIES)}> ref,ref,…[/]")
    if sub == "test":
        need = rest[0] if rest and rest[0] in CAPABILITIES else "rtl_author"
        echo(f"[bold]primary pick[/] for need={need}: "
             f"{r.pick(need) or '[dim](pool empty)[/]'}")
        for cap in CAPABILITIES:
            echo(f"  [dim]{cap}:[/] {' → '.join(r.chain(cap)) or '[dim](none)[/]'}")
        return
    if sub == "stats":
        agg = telemetry_stats(c.ws.dot)
        if not agg:
            return echo("[dim]no telemetry yet — enable: /router layer telemetry on[/]")
        for _, a in sorted(agg.items()):
            echo(f"  [bold]{a['model']}[/] / {a['task_class']}: "
                 f"{a['completed']}/{a['runs']} done"
                 f"{', ' + str(a['refused']) + ' refused' if a['refused'] else ''}"
                 f"{', ' + str(a['timed_out']) + ' timeout' if a['timed_out'] else ''}")
        return
    echo("[yellow]/router [status|on|off|layer|triggers|pool|profile|chain|test|stats][/]")


@cli.command()
@click.option("-p", "--prompt", default=None, help="headless one-shot prompt")
@click.option("--model", default=None, help="override model id")
@click.option("--provider", default=None, help="override provider (anthropic/openai/ollama/lmstudio/…)")
@click.option("--max-steps", default=40)
@click.option("--plan", "plan_mode", is_flag=True, default=None,
              help="plan mode: approve each edit/job before it runs")
@click.pass_context
def agent(ctx, prompt, model, provider, max_steps, plan_mode):
    """Start the interactive session (same as running `chipchamp` with no command)."""
    _interactive(ctx.obj, prompt=prompt, model=model, provider=provider,
                 max_steps=max_steps, plan_mode=plan_mode)


# ---- interactive session (the default surface, like claude/codex) -----------

# subcommands that don't make sense as in-session slash commands
_SLASH_EXCLUDE = {"agent", "mcp-serve"}


_LED_KINDS = ("lint", "sim", "synth")


def _board_leds(c, loop):
    """Dev-board LED strip for the input toolbar: one LED per job kind, lit
    from the workspace's LATEST job of that kind (green pass / red fail /
    yellow running / dark never-ran), plus the live model. Job records are
    TTL-cached — the toolbar re-renders on every keystroke."""
    import html as _html
    import time as _time
    now = _time.time()
    cache = getattr(_board_leds, "_cache", None)
    if cache and cache[0] > now - 3.0:
        latest = cache[1]
    else:
        latest = {}
        try:
            for rec in c.runner.list_jobs():  # ascending → later wins
                latest[rec.kind] = rec.status
        except Exception:
            pass
        _board_leds._cache = (now, latest)
    kinds = list(_LED_KINDS) + sorted(
        k for k in latest if k in ("formal", "lec"))
    cells = []
    for k in kinds:
        st = latest.get(k)
        if st == "passed":
            led = "<ansigreen>●</ansigreen>"
        elif st in ("failed", "error", "timeout"):
            led = "<ansired>●</ansired>"
        elif st == "running":
            led = "<ansiyellow>●</ansiyellow>"
        else:
            led = '<style fg="#555555">○</style>'
        cells.append(f'<style fg="#777777">{k}</style> {led}')
    model = ""
    try:
        model = (loop.gateway.model or "").rsplit("/", 1)[-1][:24]
    except Exception:
        pass
    if model:
        cells.append(f'<style fg="#666666">{_html.escape(model)}</style>')
    return "  ".join(cells)


def _print_failure_waves(c, jid: str) -> None:
    """Render a failing sim's VCD tail inline (logic-analyzer view). Pure
    diagnosis candy layered over the real job record — any failure to render
    must be silent, never a session error."""
    from . import ui
    try:
        store = c.resolve_wave(jid)
        if store is None:
            return
        from .waves.render import analyzer_view
        for ln in analyzer_view(store, width=ui.console().size.width):
            ui.console().print(ln)
    except Exception:
        pass


_SPARK = "▁▂▃▄▅▆▇█"


def _burndown(hist: list[int]) -> str:
    """Lint-error history as a sparkline: `8▇ 3▃ 0▁`, red while burning,
    green at zero — converging vs grinding at a glance."""
    mx = max(hist) or 1
    cells = []
    for v in hist[-12:]:
        ch = _SPARK[min(7, round(v / mx * 7))]
        col = "green" if v == 0 else "red"
        cells.append(f"[{col}]{v}{ch}[/]")
    return " ".join(cells)


def _agent_events(c):
    """Render the agent's stream like claude-code: compact tool calls, colored
    diffs for edits, Markdown for prose (fenced SystemVerilog gets highlighted),
    tokens streaming live as the model generates (reasoning scrolls by in the
    ticker), and a rolling tail of the live log while an EDA job runs."""
    from . import ui
    state = {"think": None, "job": None, "streamed": 0, "pulse": None}

    # the runner announces each step's live log; hand it to the active tail
    def _on_step(_job_id, live_path):
        tail = state.get("job")
        if tail is not None:
            tail.attach(live_path)
    c.runner.on_step = _on_step

    def _show_diff(path):
        e = c.edits.get(path)
        if e:
            ui.diff(e["old"], e["new"], path)

    def _show_ladder():
        """Repaint the gate ladder — but only when it actually moved.

        A ladder redrawn identically after every job is noise, and noise is how
        a HUD stops being read. It also stays quiet until there is something to
        judge: on a pure investigation nothing has been edited, so every gate
        would read `missing` and the line would be a row of dots implying the
        agent is failing at work nobody asked for."""
        if not c.edits:
            return
        rep = c.gate_status()
        fp = ui.gate_fingerprint(rep)
        if not fp or fp == state.get("ladder"):
            return
        state["ladder"] = fp
        ui.console().print("  " + ui.gate_ladder(rep))

    def on_event(kind, data):
        sub = data.get("subagent")
        if sub:
            # Subagent traffic gets its own compact, LABELED path. Unlabeled,
            # a role-denied tool call renders exactly like a parent failure —
            # in the blind eFPGA triage run a burst of least-privilege
            # denials read as a platform-wide tool lockout to the human
            # watching. And a subagent's thinking_start must never spawn a
            # StreamView: it would fight the parent's live spinner.
            tag = f"  [dim]{sub} ▸[/]"
            if kind == "tool_call":
                ui.console().print(
                    f"{tag} [dim]→ {data['name']}"
                    f" {_action_desc(data['name'], data.get('input', {}))}[/]")
            elif kind == "tool_result":
                res = data.get("result", {})
                if isinstance(res, dict):
                    if res.get("denied"):
                        ui.console().print(f"{tag} [yellow]⊘ "
                                           f"{data.get('name', '')}[/]")
                    elif "error" in res:
                        ui.console().print(
                            f"{tag} [red]✗ {data.get('name', '')}: "
                            f"{str(res['error'])[:80]}[/]")
            elif kind == "job_done":
                res = data.get("result", {})
                if isinstance(res, dict) and res.get("job"):
                    ui.console().print(
                        f"{tag} [dim]▪ {res['job']} {data.get('name', '')} "
                        f"{res.get('status', 'done')}[/]")
            elif kind == "model_switch":
                ui.console().print(f"{tag} [dim]{data['from']} → "
                                   f"{data['to']} ({data.get('reason', '')})[/]")
            elif kind == "error":
                ui.console().print(f"{tag} [red]{data.get('message', '')}[/]")
            return
        if kind == "thinking_start":
            state["streamed"] = 0
            state["think"] = ui.StreamView(data.get("model", ""),
                                           pulse=state["pulse"])
            state["think"].start()
        elif kind == "delta":
            if state["think"]:
                state["think"].feed(data.get("channel", ""),
                                    data.get("chunk", ""))
        elif kind == "thinking_done":
            if state["pulse"] is not None:
                # exact usage for the call that just finished; the spinner only
                # estimates the one still streaming
                state["pulse"].add_tokens(data.get("output_tokens", 0))
            if state["think"]:
                state["think"].stop()
                # remember whether this turn's text already rendered live, so
                # the upcoming `assistant` event doesn't print it twice
                state["streamed"] = state["think"].streamed_chars
                state["think"] = None
        elif kind == "job_start":
            # a real EDA job is about to block — tail its live log while it runs
            desc = _action_desc(data["name"], data.get("input", {}))
            state["job"] = ui.JobTail(f"{data['name']} {desc}",
                                      pulse=state["pulse"])
            state["job"].start()
        elif kind == "job_done":
            if state["job"]:
                state["job"].stop()
                state["job"] = None
            res = data.get("result", {})
            if isinstance(res, dict):
                jid = res.get("job", "")
                status = res.get("status") or res.get("sim_status") \
                    or res.get("verdict") or ("error" if "error" in res else "done")
                col = "green" if status in ("passed", "pass", "done") else \
                    ("red" if status in ("failed", "error", "fail") else "yellow")
                if jid:
                    ui.console().print(
                        f"  [dim]▪[/] [bold]{jid}[/] {data['name']} "
                        f"[{col}]{status}[/]"
                        + (f" [dim]· {str(res['summary'])[:70]}[/]"
                           if res.get("summary") else ""))
                # post-mortem at a glance: a failing sim that dumped waves
                # gets its last cycles rendered inline, analyzer-style
                if (data["name"] == "sim.run" and res.get("waves_job")
                        and str(status) in ("failed", "fail", "error")):
                    _print_failure_waves(c, res["waves_job"])
            _show_ladder()
        elif kind == "budget_block":
            ui.warn(f"  ⚠ budget: {data.get('reason', '')}")
        elif kind == "compacted":
            # say it out loud: a body left the context, and the human should
            # know the model is now working from digests plus job ids
            ui.console().print(
                f"  [dim]▪ context compacted — {data['evicted_results']} old "
                f"result(s) evicted to their job ids "
                f"({data['before'] // 1000}k → {data['after'] // 1000}k chars)[/]")
        elif kind == "tools_loaded":
            ui.console().print(
                f"  [dim]▪ loaded {len(data['added'])} tool(s) from "
                f"{', '.join(data['groups'])}[/]")
        elif kind == "destructive":
            ui.warn(f"  ⚠ destructive: {data['name']} — will ask to confirm")
        elif kind == "tool_call":
            if not data["name"].startswith("plan."):  # plan renders on its result
                ui.tool_call(data["name"], data["input"])
        elif kind == "tool_result":
            name, res = data.get("name", ""), data.get("result", {})
            if not isinstance(res, dict):
                return
            if name.startswith("plan."):
                ui.plan(res.get("plan", []))
            elif res.get("denied"):
                ui.console().print(f"  [yellow]⊘ skipped {name} (not approved)[/]")
            elif "error" in res:
                ui.console().print(f"  [red]✗ {name}: {str(res['error'])[:100]}[/]")
            elif res.get("images"):
                # an MCP renderer (e.g. wavelets' render_waveform_png) handed
                # back a picture; the base64 never entered the transcript, so
                # this is the only place it becomes visible
                ui.show_images(res["images"], mode=_image_mode(c), relto=c.ws.root)
            elif name in ("fs.edit", "fs.write") and res.get("path"):
                _show_diff(res["path"])
                # the first edit is what CREATES the obligation — it decides the
                # task class, and therefore which gates are owed. Showing the
                # ladder here means the bill arrives with the change, not at
                # report.done when it is too late to plan around.
                _show_ladder()
            elif name == "lint.run" and isinstance(res.get("errors"), int):
                hist = state.setdefault("lint_hist", [])
                hist.append(res["errors"])
                n = res["errors"]
                col = "green" if n == 0 else "red"
                line = (f"  [dim]▪[/] [bold]{res.get('job', 'lint')}[/] "
                        f"lint [{col}]{n} error(s)[/]")
                if len(hist) >= 2:  # one point isn't a burndown yet
                    line += f"  [dim]⌁[/] {_burndown(hist)}"
                ui.console().print(line)
            elif name == "regmap.generate" and res.get("ok"):
                for w in res.get("written", []):
                    if w.get("changed"):
                        _show_diff(w["path"])
        elif kind == "model_switch":
            ui.handoff(data["from"], data["to"], data.get("reason", ""))
        elif kind == "assistant" and data.get("text"):
            # already on screen if it streamed live; render only otherwise
            # (off-TTY, non-streaming provider, or a zero-content turn)
            if not state.pop("streamed", 0):
                ui.markdown(data["text"])
            state["streamed"] = 0
        elif kind == "error":
            ui.error(data["message"])
        elif kind == "security":
            kinds = ", ".join(f["kind"] for f in data["findings"][:3])
            ui.warn(f"  ⚠ injection-shaped content in {data['tool']} output "
                    f"({kinds}) — treated as data, policy unaffected")

    def shutdown():
        """Stop any live renderer (ctrl-c mid-generation would otherwise leave
        a daemon thread repainting over the prompt forever)."""
        for key in ("think", "job"):
            if state.get(key):
                state[key].stop()
                state[key] = None

    def begin_task(text: str, effort: str = ""):
        """Open the task-level clock the step spinners report against."""
        state["pulse"] = ui.TaskPulse(text, effort)
        return state["pulse"]

    def end_task():
        state["pulse"] = None

    on_event.shutdown = shutdown
    on_event.begin_task = begin_task
    on_event.end_task = end_task
    return on_event


def _session_commands(c=None) -> dict:
    """name -> menu entry, for the slash completion menu. Built-in commands map
    to a plain help string; discovered skills map to (help, "skill") so the menu
    can set them apart (ui.menu_entry) instead of rendering both identically.

    Built-ins keep their name on a collision, and the shadowed skill is offered
    as ``/skill:<name>`` — it used to just vanish from the menu."""
    cmds = {n: (c_.get_short_help_str(60) or "")
            for n, c_ in cli.commands.items() if n not in _SLASH_EXCLUDE}
    cmds.update({"help": "show commands", "model": "switch provider/model",
                 "effort": "reasoning effort: low/medium/high · off pins · auto unpins",
                 "timeout": "per-model call budget (raise it for a slow model)",
                 "router": "auto model-routing: on/off, pool, profiles, chains",
                 "mode": "cycle autonomy: normal/auto/plan (shift-tab)",
                 "plan": "toggle plan mode (approve edits/jobs)",
                 "sessions": "list / resume saved sessions",
                 "undo": "revert the files the last task changed",
                 "clear": "reset the conversation", "exit": "quit the session"})
    if c is not None:
        try:
            from .skills import discover, first_sentence
            sk = discover(c.ws)
            if sk:
                cmds.setdefault("skill", "load a skill playbook into the conversation")
                for s in sk.values():
                    key = s.name if s.name not in cmds else f"skill:{s.name}"
                    cmds.setdefault(key,
                                    (first_sentence(s.description)[:60], "skill"))
        except Exception:
            pass  # a broken skills dir must not break the REPL
    return dict(sorted(cmds.items()))


_GATE_PERMS = {"write", "submit"}
_PLAN_SUFFIX = (
    "PLAN MODE is ON: file writes/edits and job-launching tools require the "
    "human's approval before they run. Keep your plan.update checklist current "
    "and expect to be gated on each mutating action; if an action is declined, "
    "revise the plan rather than retrying it.")


def _apply_plan_mode(loop, on: bool) -> None:
    loop.gate_permissions = set(_GATE_PERMS) if on else set()
    loop.system_suffix = _PLAN_SUFFIX if on else ""


# ---- autonomy modes (item 3): cycled with shift-tab or /mode ----------------
# Destructive actions are ALWAYS confirmed regardless of mode (item 8).
_MODE_ORDER = ["normal", "auto", "plan"]
_MODE_DESC = {
    "normal": "edits & jobs run; destructive actions confirmed",
    "auto":   "full autonomy; only destructive actions confirmed",
    "plan":   "approve every edit & job before it runs",
}
# one color per mode, everywhere a mode name renders (echoes + ptk toolbar):
# green = safest (everything approved), cyan = house accent, yellow = caution
_MODE_COLOR = {"normal": "cyan", "auto": "yellow", "plan": "green"}


class _AutonomyMode:
    """Session autonomy state shared by the input controller (shift-tab) and
    the /mode command; applies itself to the loop's gating on every change."""

    def __init__(self, loop, name: str = "normal"):
        self.loop = loop
        self.name = name if name in _MODE_ORDER else "normal"
        self.apply()

    @property
    def color(self) -> str:
        """Per-mode accent color; ui.InputController colors the toolbar with it."""
        return _MODE_COLOR[self.name]

    def apply(self) -> None:
        _apply_plan_mode(self.loop, self.name == "plan")

    def cycle(self) -> str:
        self.name = _MODE_ORDER[(_MODE_ORDER.index(self.name) + 1) % len(_MODE_ORDER)]
        self.apply()
        return self.name

    def set(self, name: str) -> bool:
        if name not in _MODE_ORDER:
            return False
        self.name = name
        self.apply()
        return True


def _action_desc(name, args) -> str:
    a = args or {}
    for key in ("path", "test", "tag", "top", "module", "spec", "branch"):
        if key in a:
            return f"{key}={a[key]}"
    return _short(a)


def _make_approver():
    """Interactive approval. Two paths:
      • destructive ops (perm='destructive') — ALWAYS confirmed individually,
        with a stronger prompt, never covered by an earlier 'approve all', in
        every autonomy mode (item 8);
      • plan-mode gating of writes/jobs — y / a(ll) / N.
    Off a TTY both decline (safe: a headless run can't delete or auto-apply)."""
    from . import ui
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    allow = {"all": False}

    def approve(name, args, perm):
        destructive = perm == "destructive"
        if allow["all"] and not destructive:
            return True  # 'approve all' never covers destructive actions
        if destructive:
            ui.warn(f"  ⚠ DESTRUCTIVE: {name}  {_action_desc(name, args)}")
            if not tty:
                ui.warn("     non-interactive → declining (destructive actions "
                        "need an interactive confirm).")
                return False
            try:
                ans = input("     permanently proceed? type 'yes' to confirm: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            ok = ans == "yes"
            ui.info("     [green]confirmed[/]" if ok else "     [dim]cancelled[/]")
            return ok
        ui.approval_request(name, _action_desc(name, args))
        if not tty:
            ui.warn("  non-interactive + plan mode → declining (preview only); "
                    "switch to normal/auto mode (shift-tab or /mode) to apply.")
            return False
        try:
            ans = input("     approve? [y]es once · [a]ll · [N]o : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans == "a":
            allow["all"] = True
            ui.info("     [dim]approving all further (non-destructive) actions[/]")
            return True
        ok = ans in ("y", "yes")
        ui.info("     [green]approved[/]" if ok else "     [dim]skipped[/]")
        return ok

    return approve


def _undo_cmd(c) -> None:
    """`/undo` — put the tree back the way it was before the last task."""
    out = c.undo()
    if out.get("error"):
        echo(f"[yellow]{out['error']}[/] [dim]— nothing has been edited yet "
             f"this session[/]")
        return
    for p in out["restored"]:
        echo(f"  [green]reverted[/] {p}")
    for p in out["deleted"]:
        echo(f"  [green]removed[/]  {p} [dim](created by that task)[/]")
    for p in out["skipped"]:
        echo(f"  [yellow]kept[/]     {p} [dim](changed on disk since — your "
             f"edit, not the agent's)[/]")
    if not (out["restored"] or out["deleted"] or out["skipped"]):
        echo("[dim]nothing to undo — that task changed no files[/]")
    else:
        echo(f"[dim]undone: {out.get('checkpoint') or 'last task'}[/]")


def _image_mode(c) -> str:
    """`[ui] mcp_images` — how a picture from an MCP tool is presented.

    auto (default) draws it when the terminal has an inline protocol and links
    it otherwise; `image` is the same but says why it could not draw; `off`
    always links. Text chronograms are a different TOOL, not a fallback, so
    `off` is a preference for the rendering that survives asciinema, SSH and
    tmux rather than a degraded mode."""
    mode = str((c.ws.config.get("ui", {}) or {}).get("mcp_images", "auto")).lower()
    return mode if mode in ("auto", "image", "off") else "auto"


def _context_budget(c, gw) -> int:
    """Transcript budget for this session, or 0 to never compact.

    Off for a NullGateway (there is no run to protect) and honours an explicit
    `[model] context_chars = 0` for anyone who would rather hit a hard wall
    than have a tool result evicted behind their back."""
    if gw is None:
        return 0
    from .agent.working_set import budget_for
    return budget_for(gw, c.ws.config)


def _make_budget_approver(c):
    """FR-BUDG-01: a ceiling pauses and ASKS. Interactive sessions can approve
    an overrun; a headless run cannot, and stops — which is the whole point of
    running one unattended."""
    from . import ui

    def approve(kind: str, why: str) -> bool:
        ui.warn(f"  [bold yellow]budget[/] {why}")
        if not sys.stdin.isatty():
            ui.warn("  non-interactive → stopping at the ceiling "
                    f"(raise [bold]{kind}[/] in .chipchamp/config.toml to go on)")
            return False
        try:
            ans = input("     raise it for this task? type 'yes': ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        ok = ans == "yes"
        if ok:
            # one task's reprieve, not a new ceiling: the budget in config is
            # still what tomorrow's run gets
            b = c.ledger.budget
            if kind == "license_hours":
                b.license_hours += 4.0
            elif kind == "v3_submissions":
                b.v3_submissions += 1
        ui.info("     [green]raised for this task[/]" if ok
                else "     [dim]stopped at the ceiling[/]")
        return ok

    return approve


def _session_user_inputs(sess) -> list[str]:
    """Genuine user requests from a session, oldest→newest, excluding the loop's
    synthetic nudge turns. New sessions flag real inputs (`input: True`); for
    older sessions predating the flag, fall back to every text user turn."""
    msgs = [m for m in sess.messages if isinstance(m, dict)]
    marked = any("input" in m for m in msgs)
    out = []
    for m in msgs:
        if m.get("role") != "user" or not isinstance(m.get("content"), str):
            continue
        if marked and not m.get("input"):
            continue  # a synthetic nudge, not the user
        s = m["content"].strip()
        if s:
            out.append(s)
    return out


def _replay_session(sess, max_turns: int = 40) -> None:
    """Print a resumed session's prior conversation so the context is visible,
    not just referenced. Renders genuine user turns and assistant replies;
    tool-result turns and synthetic nudges are skipped."""
    from rich.markup import escape

    from . import ui
    msgs = [m for m in sess.messages if isinstance(m, dict)]
    marked = any("input" in m for m in msgs)
    turns = []
    for m in msgs:
        role, content = m.get("role"), m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            if marked and not m.get("input"):
                continue
            turns.append(("user", content.strip()))
        elif role == "assistant":
            turns.append(("assistant", content.strip()))
    if not turns:
        return
    shown = turns[-max_turns:]
    echo("[dim]──────── previous conversation ────────[/]")
    if len(turns) > len(shown):
        echo(f"[dim]  (… {len(turns) - len(shown)} earlier turns omitted)[/]")
    for role, content in shown:
        if role == "user":
            echo(f"[cyan]»[/] {escape(content)}")
        else:
            ui.markdown(content)
    echo("[dim]───────────────────────────────────────[/]")


def _interactive(ctx_obj, prompt=None, model=None, provider=None, max_steps=40,
                 plan_mode=None, resume=None, experiment=None):
    from . import ui
    from .agent import AgentLoop, NullGateway, Session
    from .mcp import load_mcp_tools
    from .tools import all_tools as _all
    c = _ctx(ctx_obj["root"], ctx_obj["target"])
    c.ws.ensure_work_dir()  # the agent's scratch/design dir (item: work dir)
    gw, reason, audit = _build_gateway(c, model, provider)
    has_model = gw is not None
    if not has_model:
        gw = NullGateway()

    # plan mode default: explicit --plan, else on when the policy's default
    # autonomy is L0 ("suggest") — the human applies everything.
    if plan_mode is None:
        plan_mode = c.policy.default_autonomy == "L0"

    mcp_tools, mcp_clients = load_mcp_tools(
        c.ws.config, cwd=c.ws.root,
        image_dir=str(c.ws.dot / "mcp-images"))
    tool_set = {**_all(), **mcp_tools} if mcp_tools else None
    store = str(c.ws.dot / "sessions")
    sess, resumed = None, None
    if resume == "__latest__":
        sess = Session.latest(store)
        resumed = "most recent" if sess else None
    elif resume:
        sess = Session.load(resume, store)
        resumed = resume if sess else None
        if sess is None:
            echo(f"[yellow]no session '{resume}' in this workspace[/] — "
                 f"starting fresh (`/sessions` lists them)")
    if sess is None:
        sess = Session.new(store, target=c.target_name)
    events = _agent_events(c)
    # Progressive tool disclosure: on by default for local servers, where the
    # context window is the binding constraint (the full catalog is ~8.4k
    # tokens of schema on every request; the core set is ~2.1k). Frontier
    # models keep the whole catalog unless the workspace says otherwise.
    disclose = c.ws.config.get("model", {}).get(
        "tool_disclosure",
        str(getattr(gw, "ref", "")).startswith(("ollama:", "lmstudio:")))
    c.budget_approver = _make_budget_approver(c)
    loop = AgentLoop(c, gw, session=sess, max_steps=max_steps,
                     on_event=events, tools=tool_set,
                     approver=_make_approver(), router=_build_router(c),
                     disclose=disclose, context_budget=_context_budget(c, gw))
    mode = _AutonomyMode(loop, "plan" if plan_mode else "normal")

    # headless one-shot (`chipchamp -p "..."`)
    if prompt:
        if not has_model:
            echo(f"[yellow]No model configured:[/] {reason}")
            raise SystemExit(2)
        rec = None
        if experiment:
            from .bench.experiment import ExperimentRecorder
            rec = ExperimentRecorder(c.ws.dot, label=experiment, task=prompt)
            rec.attach(loop)
        out = loop.run(prompt)
        sess.save()
        if rec is not None:
            exp = rec.finish(out, max_steps=max_steps)
            echo(f"[dim]— experiment [bold]{exp.id}[/] ({exp.outcome}) "
                 f"recorded under {c.rel(rec.dir)}[/]")
        if out.get("text"):
            echo(out["text"])
        extras = "".join(
            f", {out[k]} {lbl}" for k, lbl in
            (("recovered_calls", "recovered-calls"),
             ("empty_turns", "empty-turns"),
             ("length_stops", "length-stops")) if out.get(k))
        echo(f"[dim]— {out['steps']} steps, {out.get('tokens', 0)} tokens{extras}, "
             f"model {out.get('model', '?')}, "
             f"jobs: {', '.join(out.get('jobs', [])) or 'none'} — audit: {c.rel(audit)}[/]")
        return

    ui.clear()  # open as its own app (TTY only)
    _print_banner(c, gw, has_model, reason, len(mcp_tools) if mcp_tools else 0,
                  ctx_obj.get("root_how", ""), plan_on=bool(loop.gate_permissions),
                  mode=mode)
    if resumed:
        echo(f"  [green]resumed[/] {resumed} session [bold]{sess.id}[/] "
             f"[dim]({len(sess.messages)} messages) — /sessions to switch[/]")
        _replay_session(sess)
    inp = ui.InputController(
        _session_commands(c), mode=mode,
        on_mode_change=lambda name: echo(
            f"  [dim]autonomy → [/][bold {_MODE_COLOR[name]}]{name}[/] "
            f"[dim]{_MODE_DESC[name]}[/]"),
        toolbar_extra=lambda: _board_leds(c, loop))
    if resumed:  # make the resumed session's requests recallable with up/down
        inp.remember(_session_user_inputs(sess))
    pending: list[str] = []  # /skill playbooks staged for the next task
    while True:
        inp.separator()
        try:
            line = inp.read()
        except (EOFError, KeyboardInterrupt):
            echo("")
            break
        if not line:
            continue
        if line.startswith("/"):
            if _dispatch_slash(ctx_obj, c, loop, line,
                               pending if has_model else None, mode=mode,
                               inp=inp) == "exit":
                break
            continue
        if line in ("exit", "quit"):
            break
        if not has_model:
            ui.warn("No model configured, so plain-English tasks can't run. "
                    "Use /model to pick one, or slash-commands like /sim, /lint, "
                    "/triage (they work without a model).")
            continue
        # the status line names the task in the user's own words, so it is
        # built BEFORE any staged skill playbook is prepended to the prompt
        events.begin_task(line, getattr(loop.gateway, "reasoning_effort", ""))
        # mark the tree so `/undo` can put this one task back, and only this
        # one — an undo that unwound three tasks at once would be unusable
        c.checkpoint(ui.task_label(line, 60))
        if pending:
            line = "\n\n".join(pending) + f"\n\n---\nTask: {line}"
            pending.clear()
        try:
            out = loop.run(line)
            # a too-slow model: offer a bigger-budget retry instead of just
            # losing the turn (loop only flags this once the router can't swap).
            while out.get("timed_out") and \
                    _offer_timeout_retry(c, loop, line, out) == "retry":
                out = loop.run(line)
        except KeyboardInterrupt:
            events.shutdown()  # stop live renderers before touching the screen
            echo("[dim](interrupted)[/]")
        finally:
            events.end_task()
        sess.save()
    for mc in (mcp_clients or []):
        mc.close()


def _git_rev(root: str) -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# The package is FILLED, so the die reads as a solid part rather than an
# outline. grey27 is the only fill that survives both extremes: dark enough to
# look like epoxy (9.7:1 against a white terminal) and light enough to stay
# visible on a black one (1.7:1) — grey15 vanishes on a dark theme (1.01:1
# against solarized-dark). The silkscreen is lifted to match: bright_black
# only manages 2.5:1 on the fill, grey70 makes 4.1:1 and cyan 7.8:1.
_CHIP_FILL = "grey27"
_CHIP_LEAD = "grey37"                    # leads, sit on the terminal background
# silkscreen for a dark package / for a light one: (outline, stamp, name)
_CHIP_INK = {False: ("grey62", "grey70", "bold cyan"),
             True: ("grey11", "grey23", "bold #005f87")}


def _chip_ink(fill: str) -> tuple[str, str, str, str]:
    """(fill, outline, stamp, name) for a package fill. The ink follows the
    fill's luminance — light on a dark package, dark on a light one — so a
    configured colour stays legible instead of disappearing into its own body.
    An unparseable fill falls back: the banner must never fail to render."""
    try:
        from rich.color import Color as _RColor
        r, g, b = _RColor.parse(fill).get_truecolor()
    except Exception:
        fill, (r, g, b) = _CHIP_FILL, (68, 68, 68)
    lin = [c / 255 for c in (r, g, b)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in lin]
    light = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2] > 0.18
    return (fill, *_CHIP_INK[light])


def _chip_banner(proj: str, root: str, fill: str = "") -> list[str]:
    """The session opens on a die shot: the workspace is the chip, silkscreened
    with its name and stamped lot-code style (date + git rev) — the tool reads
    as a piece of hardware from the first frame.

    The package is filled (``[ui] chip_fill`` overrides the default epoxy
    grey), so it reads as a solid part rather than an outline."""
    import time as _time
    name = (proj or _brand.APP_NAME)[:13]
    yw = _time.strftime("%y%W")  # year+week, like real silicon lot codes
    rev = _git_rev(root)[:7]
    stamp = (f"{yw} · {rev}" if rev else f"lot {yw}")[:15]
    inner = 15
    fill, outline, ink, silk = _chip_ink(fill or _CHIP_FILL)
    edge = f"{outline} on {fill}"

    def row(text: str, style: str) -> str:
        """One package row: lead, wall, silkscreen, wall, lead. The fill runs
        wall to wall — centring inside the styled span keeps the padding filled
        too, so the body stays a solid rectangle."""
        return (f"    [{_CHIP_LEAD}]─[/][{edge}]┤[/]"
                f"[{style} on {fill}]{text:^{inner}}[/]"
                f"[{edge}]├[/][{_CHIP_LEAD}]─[/]")

    return [
        f"     [{edge}]┌{'┬' * inner}┐[/]",
        row(f"{_brand.APP_NAME} {__version__}", ink),
        row(name, silk),
        row(stamp, ink),
        f"     [{edge}]└{'┴' * inner}┘[/]",
    ]


def _print_banner(c, gw, has_model, reason, n_mcp, how="", plan_on=False,
                  mode=None):
    """Die shot on the left, session info silkscreened beside it — one compact
    block instead of the chip floating above a paragraph."""
    from rich.text import Text
    proj = c.ws.config.get("project", {}).get("name") or c.ws.default_target
    nmods = len(c.ws.db().modules)
    chip = _chip_banner(proj, c.ws.root,
                        fill=c.ws.config.get("ui", {}).get("chip_fill", ""))
    info = ["[dim]agentic RTL design & verification[/]",
            f"[dim]repo[/] {c.ws.root}" + (f" [dim]({how})[/]" if how else "")]
    line = f"[dim]target[/] {c.target_name} [dim]({nmods} modules)[/]"
    if has_model:
        line += f"  [dim]·  model[/] {gw.ref}"
    if n_mcp:
        line += f"  [dim]·  mcp[/] {n_mcp} tool(s)"
    if plan_on:
        line += "  [dim]·[/] [yellow]plan mode[/]"
    info.append(line)
    if has_model:
        hint = "[dim]Type a task in plain English.  /help for commands · /exit to quit[/]"
        if plan_on:
            hint = ("[dim]Plan mode: I'll show a plan and ask before edits/jobs. "
                    "/plan off to disable · /help · /exit[/]")
        info.append(hint)
    else:
        info.append(f"[yellow]no model:[/] [dim]{reason}[/]")
        info.append("[dim]/model to pick one · slash-commands (/sim /lint /triage) "
                    "work without a model · /help[/]")
    if mode is not None:
        info.append(f"[dim]autonomy:[/] [bold {mode.color}]{mode.name}[/] "
                    f"[dim]— {_MODE_DESC[mode.name]} · shift-tab or /mode[/]")
    echo("")
    col = max(Text.from_markup(ln).cell_len for ln in chip) + 2
    for i in range(max(len(chip), len(info))):
        left = chip[i] if i < len(chip) else ""
        right = info[i] if i < len(info) else ""
        pad = " " * (col - (Text.from_markup(left).cell_len if left else 0))
        # The two halves are printed as one Text so they can be highlighted
        # differently: the chip is a hand-styled graphic and the repr
        # highlighter must not recolor its version digits, while the info
        # beside it keeps the highlighting it has always had.
        out = Text.from_markup(left)
        if right:
            out.append(pad)
            out.append_text(_con.render_str(right, highlight=True) if _con
                            else Text.from_markup(right))
        echo(out)
    echo("")


def _dispatch_slash(ctx_obj, c, loop, line, pending=None, mode=None,
                    inp=None) -> str | None:
    import shlex
    try:
        parts = shlex.split(line[1:])
    except ValueError:
        parts = line[1:].split()
    if not parts:
        return None
    name, args = parts[0], parts[1:]
    if name in ("exit", "quit", "q"):
        return "exit"
    if name == "skill":
        if not args or args[0] == "list":
            _print_skills(c)
        else:
            _load_skill(c, loop, args[0], pending)
        return None
    if name.startswith("skill:"):
        # qualified form for a skill shadowed by a built-in of the same name
        _load_skill(c, loop, name[len("skill:"):], pending)
        return None
    if name == "sessions":
        _sessions_cmd(c, loop, args, inp=inp)
        return None
    if name in ("help", "?", "h"):
        _repl_help(c)
        return None
    if name == "clear":
        loop.session.messages = []
        loop.session.save()
        echo("[dim]conversation cleared[/]")
        return None
    if name == "undo":
        _undo_cmd(c)
        return None
    if name == "mode":
        if mode is None:
            echo("[yellow]autonomy modes aren't available here[/]")
            return None
        if args and args[0].lower() in _MODE_ORDER:
            mode.set(args[0].lower())
        else:
            mode.cycle()
        echo(f"[dim]autonomy → [/][bold {mode.color}]{mode.name}[/] "
             f"[dim]{_MODE_DESC[mode.name]}[/]")
        return None
    if name == "plan":
        # kept for muscle memory; plan is one of the three autonomy modes now
        on = not (mode.name == "plan" if mode else bool(loop.gate_permissions))
        if args and args[0].lower() in ("on", "off"):
            on = args[0].lower() == "on"
        if mode is not None:
            mode.set("plan" if on else "normal")
        else:
            _apply_plan_mode(loop, on)
        echo(f"[green]plan mode {'on' if on else 'off'}[/]"
             + (f" [dim]— autonomy:[/] [{mode.color}]{mode.name}[/]" if mode else ""))
        return None
    if name == "model":
        if args and args[0] in ("tune", "opts", "params"):
            _model_tune_cmd(c, list(args[1:]), loop=loop)
            return None
        if args and args[0] in ("showconfig", "config", "show"):
            _show_model_config(c)
            return None
        _select_model(c)
        gw2, _, _ = _build_gateway(c)
        if gw2:
            loop.gateway = gw2
            echo(f"[green]switched to {gw2.ref}[/]")
        return None
    if name == "router":
        _router_cmd(c, args, loop=loop)
        return None
    if name == "effort":
        _effort_cmd(c, args, loop=loop)
        return None
    if name == "timeout":
        _timeout_cmd(c, args, loop=loop)
        return None
    if name in _SLASH_EXCLUDE:
        echo(f"[yellow]/{name} is not available in the session[/]")
        return None
    cmd = cli.get_command(None, name)
    if cmd is None:
        # not a built-in: a discovered skill name dispatches as /skill <name>
        # (built-ins always win; /skill <name> is the shadowed-skill escape)
        try:
            from .skills import discover
            if name in discover(c.ws):
                _load_skill(c, loop, name, pending)
                return None
        except Exception:
            pass
        echo(f"[yellow]unknown command /{name}[/] — try [bold]/help[/] "
             f"or [bold]/skill list[/]")
        return None
    try:
        cmd.main(args=args, prog_name=f"/{name}", standalone_mode=False, obj=ctx_obj)
    except SystemExit:
        pass  # e.g. /ci exits, or /<cmd> --help
    except click.ClickException as e:
        e.show()
    except click.exceptions.Abort:
        echo("[dim](aborted)[/]")
    except Exception as e:  # a command crash must not kill the session
        echo(f"[red]/{name} failed: {type(e).__name__}: {e}[/]")
    return None


def _sessions_cmd(c, loop, args, inp=None) -> None:
    """`/sessions` lists this workspace's saved sessions; `/sessions resume
    <id>` (or `--resume <id>`) loads one into the running conversation."""
    import time as _time

    from .agent import Session
    store = str(c.ws.dot / "sessions")
    a = [x for x in args if x not in ("--resume", "resume")]
    asked_resume = "resume" in args or "--resume" in args
    if asked_resume and not a:  # `/sessions resume` with no id — don't silently list
        echo("[yellow]/sessions resume <id>[/] — give a session id "
             "(`/sessions` lists them)")
        return
    if (asked_resume or (a and a[0].startswith("S-"))) and a:
        sid = a[0]
        s = Session.load(sid, store)
        if s is None:
            echo(f"[yellow]no session '{sid}'[/] — /sessions lists them")
            return
        loop.session = s
        echo(f"[green]resumed[/] {sid} [dim]({len(s.messages)} messages) — the "
             f"next task continues with its context[/]")
        _replay_session(s)
        if inp is not None:  # make its requests recallable with up/down now
            inp.remember(_session_user_inputs(s), live=True)
        return
    rows = Session.summaries(store)
    if not rows:
        echo("[dim]no saved sessions in this workspace yet[/]")
        return
    echo("[bold]sessions[/] [dim](newest first — /sessions resume <id>)[/]")
    for r in rows:
        cur = " [green]● current[/]" if loop.session and r["id"] == loop.session.id else ""
        when = _time.strftime("%m-%d %H:%M", _time.localtime(r["mtime"]))
        echo(f"  [bold]{r['id']}[/]  [dim]{when} · {r['messages']} msg · "
             f"{r['target']}[/]{cur}")
        if r["preview"]:
            echo(f"      [dim]{r['preview']}[/]")


def _print_skills(c) -> None:
    from .skills import discover_report, first_sentence
    rep = discover_report(c.ws)
    if not rep.skills:
        echo("[dim]no skills found — point a directory with[/] "
             "[bold]/skills add <dir>[/]")
        return
    hidden = []
    for s in rep.skills.values():
        # a skill named like a built-in can't own /<name>; say so here rather
        # than letting it look absent from the menu
        clash = s.name in cli.commands
        if clash:
            hidden.append(s.name)
        echo(f"  [bold]{s.name:24}[/] {first_sentence(s.description)[:76]} "
             f"[dim]({s.source})[/]")
    echo(f"[dim]{len(rep.skills)} skill(s) — /skill <name> loads one; the "
         f"agent also loads them itself via skill.use[/]")
    for n in hidden:
        echo(f"[yellow]  /{n} runs the built-in command[/] [dim]— use[/] "
             f"[bold]/skill:{n}[/] [dim]for the skill[/]")
    for w in rep.shadowed:
        echo(f"[yellow]  shadowed: {w}[/]")


def _load_skill(c, loop, name, pending) -> None:
    """Manual skill invocation: stage the playbook as context for the next
    plain-English task (loop.run takes one user string; prepending keeps the
    transcript and provider translation untouched)."""
    from . import ui
    from .skills import discover, skill_body
    sk = discover(c.ws)
    s = sk.get(name)
    if s is None:
        import difflib
        close = difflib.get_close_matches(name, list(sk), n=3, cutoff=0.5)
        echo(f"[yellow]unknown skill '{name}'[/]"
             + (f" — did you mean: {', '.join(close)}?" if close else ""))
        return
    body = skill_body(s)
    # manual path bypasses the loop's tool-result scanner — scan here (FR-SEC-05)
    try:
        from .policy.injection import scan_text
        findings = scan_text(body["body"])
        if findings:
            ui.warn(f"  ⚠ injection-shaped content in skill '{name}' — "
                    f"loaded as data, policy unaffected")
    except Exception:
        pass
    n_lines = body["body"].count("\n") + 1
    if pending is None:
        # no session to stage into (e.g. no model): just show it
        ui.markdown(body["body"])
        return
    pending.append(f"[Skill playbook: {name} — follow where applicable]\n"
                   + body["body"])
    extra = f", {len(body['files'])} reference file(s)" if body["files"] else ""
    echo(f"[green]loaded skill '{name}'[/] [dim]({n_lines} lines{extra}) — "
         f"applies to your next message[/]")


_HELP_GROUPS = {
    "explore": ["hier", "module", "cone", "fsm", "cdc", "diagram",
                "index", "tools", "adapters", "packs"],
    "verify": ["sim", "lint", "lint-burndown", "cov", "triage", "ci", "bench"],
    "implement": ["uvm", "regmap", "synth", "optimize", "pnr", "fpga", "pins",
                  "efpga-fabulous", "riscv"],
    "report": ["jobs", "repro", "dashboard"],
    "setup": ["workspace", "library", "skills"],
}

# One hue per category, so the eye can jump to a section instead of reading
# every heading. Follows the flow of the work — look, prove, build, report —
# and reuses the meanings the rest of the UI already assigns: green passes
# (verify), yellow is the caution/config end (setup), cyan is the house accent.
_HELP_COLOR = {"explore": "cyan", "verify": "green", "implement": "magenta",
               "report": "blue", "setup": "yellow", "libraries": "blue",
               "session": "cyan", "more": "grey62", "skills": "#5f00af"}


def _head(name: str) -> str:
    """A /help section heading in its category color."""
    return f"[bold {_HELP_COLOR.get(name, 'cyan')}]{name}[/]"

# name -> "args — description", one per line. `\[` keeps rich from reading an
# argument hint like [on|off] as markup and swallowing it.
_HELP_SESSION = {
    "mode": " — cycle autonomy (shift-tab): normal · auto · plan",
    "plan": r" \[on|off] — plan mode (approve edits/jobs)",
    "sessions": r" \[resume <id>] — list or resume a saved session "
                "(also `chipchamp --continue` / `--resume <id>` at launch)",
    "skill": " <name> — load a skill playbook for the next task "
             "(or just /<skill-name>, /skill:<name> if a command owns it); "
             "/skill list shows them",
    "model": r" \[tune <key>=<value>] — switch model, or tune its "
             "generation params (num_ctx, temperature, top_p, seed, …)",
    "effort": r" \[low|medium|high|off|auto] — reasoning effort "
              "(reasoning models: gpt-oss, gpt-5, o-series; off pins, auto unpins)",
    "timeout": r" \[<seconds>|Nm|off|reset] — per-model call budget; raise it "
               "for a slow model (streaming: max gap between tokens)",
    "router": r" \[on|off|status|pool|chain|…] — auto model-routing across models",
    "undo": " — revert the files the last task changed "
            "(anything you edited by hand since is kept)",
    "clear": " — reset conversation",
    "help": " — this list",
    "exit": " — quit",
}


def _repl_help(c=None):
    """Print the command list, derived from the live command table.

    Every name the completion menu offers is shown: the curated groups first,
    then whatever they don't cover (new commands, discovered skills) under a
    catch-all — so the help can't silently drift out of date again."""
    cmds = _session_commands(c)

    def line(msg: str = "") -> None:
        """/help is hand-styled markup end to end, so the repr highlighter is
        off: it reads every /command as a filesystem path and paints it
        magenta, which drowns the category colors it would compete with."""
        echo(msg, highlight=False)

    line("[bold]Talk to the agent[/] — just type a task, e.g. "
         "[dim]“why does fifo_smoke fail on seed 3?”[/]")
    shown = set()
    for g, names in _HELP_GROUPS.items():
        here = [n for n in names if n in cmds]
        shown.update(here)
        if here:
            line(f"{_head(g)}  " + " ".join(f"/{x}" for x in here))
    # registered IP libraries, each with its component count — so /library isn't
    # just a command name but shows what's actually available to reuse.
    if c is not None:
        try:
            from .library import discover_report
            libs = discover_report(c.ws).libraries
            if libs:
                line(f"{_head('libraries')}  " + "  ".join(
                    f"{lib['name']} [dim]({lib['count']})[/]" for lib in libs))
        except Exception:
            pass  # a broken manifest must never break /help
    line(_head("session"))
    for name, tail in _HELP_SESSION.items():
        if name in cmds or name == "skill":
            shown.add(name)
            line(f"  /{name}{tail}")
    rest = [n for n in cmds if n not in shown]
    if rest:
        # skills discovered in the workspace are commands too; keep them apart
        # from real subcommands so the split stays meaningful. The table carries
        # the kind, so this matches what the completion menu shows.
        from . import ui
        other = [n for n in rest if ui.menu_entry(cmds[n])[1] != "skill"]
        skills = [n for n in rest if ui.menu_entry(cmds[n])[1] == "skill"]
        if other:
            line(f"{_head('more')}  " + " ".join(f"/{x}" for x in other))
        if skills:
            line(f"{_head('skills')}  " + " ".join(f"/{x}" for x in skills))
    line("[dim]Any command also works from the shell: `chipchamp <command>`. "
         "Add --help to any /command for its options.[/]")




@cli.command("router", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1)
@click.pass_context
def router_cli(ctx, args):
    """Configure auto model-routing (mirrors the `/router` session command).

    Examples: `chipchamp router on`, `chipchamp router pool add
    lmstudio:qwen/qwen3.6-35b-a3b`, `chipchamp router chain debugger
    lmstudio:qwen/qwen3.6-35b-a3b`, `chipchamp router test`, `chipchamp router
    status`. Settings persist to .chipchamp/router.json and apply to
    interactive and headless (`-p`) runs alike."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _router_cmd(c, list(args))


@cli.command(epilog=_tune_epilog())
@click.option("--list", "list_only", is_flag=True, help="list available models and exit")
@click.option("--set", "set_ref", default=None, help="set selection as provider:model")
@click.option("--tune", "tune", multiple=True, metavar="KEY=VALUE",
              help="set a generation parameter for the selected model "
                   "(e.g. --tune num_ctx=8192 --tune temperature=0.7); "
                   "KEY= (empty) clears one. Repeatable.")
@click.option("--tune-global", is_flag=True, help="apply --tune to every model (*)")
@click.option("--tune-reset", is_flag=True, help="clear tuning for the selected model")
@click.option("--show-tune", is_flag=True, help="show effective tuning and exit")
@click.option("--timeout", "timeout", default=None, metavar="SECONDS|Nm|off",
              help="per-model call budget (e.g. --timeout 1200 or 20m or off), "
                   "persisted to .chipchamp/timeout.json")
@click.option("--timeout-global", is_flag=True, help="apply --timeout to every model (*)")
@click.option("--timeout-reset", is_flag=True, help="clear the selected model's budget")
@click.option("--showconfig", is_flag=True, help="show the selected model's settings "
              "as configured in ollama / lm studio, plus chipchamp's overrides")
@click.pass_context
def model(ctx, list_only, set_ref, tune, tune_global, tune_reset, show_tune,
          timeout, timeout_global, timeout_reset, showconfig):
    """Query accessible models across providers and select one (SPEC §14).

    Discovers Anthropic, OpenAI and local OpenAI-compatible servers (LM Studio,
    Ollama, vLLM, llama.cpp), lists their models, and saves your pick to
    .chipchamp/model.json. `--tune KEY=VALUE` sets generation parameters (sent
    verbatim to the server: temperature, top_p, num_ctx, seed, …), persisted to
    .chipchamp/model_opts.json."""
    from .agent import ModelRegistry
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    if set_ref:
        prov, _, mid = set_ref.partition(":")
        reg.save_selection(prov, mid)
        echo(f"[green]selected[/] {prov}:{mid}")
        return
    if showconfig:
        _show_model_config(c)
        return
    if tune or tune_reset or tune_global or show_tune:
        from .agent.model_opts import parse_kv, reset, set_opts
        ref = _current_ref(c)
        if tune_reset:
            reset(c.ws.dot, ref, glob=tune_global)
        if tune:
            if not tune_global and not ref:
                echo("[yellow]no model selected[/] — `chipchamp model --set "
                     "<provider:model>` first, or use --tune-global")
                raise SystemExit(1)
            setk, unset = parse_kv(tune)
            set_opts(c.ws.dot, ref, setk, unset, glob=tune_global)
        _model_tune_status(c, ref)
        return
    if timeout is not None or timeout_global or timeout_reset:
        from .agent.model_timeout import reset as _treset, set_timeout
        ref = _current_ref(c)
        if timeout_reset:
            _treset(c.ws.dot, ref, glob=timeout_global)
        if timeout is not None:
            secs = _parse_duration(timeout)
            if secs is None:
                echo(f"[yellow]not a duration:[/] {timeout} [dim]— give seconds "
                     "(1200), minutes (20m), or off[/]")
                raise SystemExit(1)
            if not timeout_global and not ref:
                echo("[yellow]no model selected[/] — `chipchamp model --set "
                     "<provider:model>` first, or use --timeout-global")
                raise SystemExit(1)
            set_timeout(c.ws.dot, ref, secs, glob=timeout_global)
        _timeout_status(c, ref)
        return
    if list_only:
        _list_models(reg)
        return
    _select_model(c)


def _list_models(reg):
    echo("[dim]probing providers (this can take a moment for local servers)…[/]")
    found = reg.discover()
    if not found:
        echo("[yellow]No reachable providers.[/] Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
             "or start a local server (Ollama :11434, LM Studio :1234).")
        return found
    for prov, models in found:
        loc = " [dim](local)[/]" if prov.config.local else ""
        echo(f"\n[bold]{prov.name}[/]{loc}  [dim]{prov.config.base_url}[/]")
        for m in models[:30]:
            echo(f"  {m.id}")
        if len(models) > 30:
            echo(f"  [dim]… {len(models)-30} more[/]")
    return found


def _select_model(c):
    from .agent import ModelRegistry
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    found = _list_models(reg)
    if not found:
        return
    flat = [(prov, m) for prov, models in found for m in models]
    echo("\n[bold]Select a model[/]:")
    for i, (prov, m) in enumerate(flat, 1):
        echo(f"  [cyan]{i:2}[/]  {prov.name}:{m.id}")
    try:
        choice = input("model number (or blank to cancel) » ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(flat)):
        echo("[dim]cancelled[/]")
        return
    prov, m = flat[int(choice) - 1]
    reg.save_selection(prov.name, m.id)
    echo(f"[green]selected[/] {prov.name}:{m.id} [dim](saved to .chipchamp/model.json)[/]")


@cli.command()
@click.argument("spec")
@click.option("--validate-only", is_flag=True)
@click.pass_context
def regmap(ctx, spec, validate_only):
    """Validate/regenerate register collateral from a YAML spec (P10)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    from .tools import all_tools as _t
    v = _t()["regmap.validate"].handler(c, spec=spec)
    if not v.get("ok"):
        echo(f"[red]spec invalid:[/] {v.get('errors', v.get('error'))}")
        raise SystemExit(1)
    echo(f"[green]spec ok[/] — {', '.join(v['registers'])}")
    if validate_only:
        return
    g = _t()["regmap.generate"].handler(c, spec=spec)
    for w in g.get("written", []):
        mark = "regenerated" if w["changed"] else "unchanged"
        echo(f"  {w['path']}  [dim]{mark} ({w['sha256']})[/]")


@cli.command()
@click.option("--suite", default="b1", type=click.Choice(["b1", "b6", "all"]))
@click.option("--mutants", default=6)
@click.pass_context
def bench(ctx, suite, mutants):
    """Run ChipchampBench on the workspace (B1 mutation-detection, B6 triage
    fidelity). Results land in .chipchamp/bench/."""
    import tempfile

    from .bench import run_b1, run_b6
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    out_dir = c.ws.dot / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="chipchamp-bench-") as work:
        if suite in ("b1", "all"):
            echo("[bold]B1 mutation-detection[/] (injecting real bugs, running the smoke test)")
            r = run_b1(c.ws.root, work, max_mutants=mutants,
                       on_event=lambda m: echo(
                           f"  {'[green]killed[/]' if m['detected'] else '[red]SURVIVED[/]'} "
                           f"{m['mutant']} @ line {m['line']}"))
            reports.append(r)
            echo(f"  detection rate: [bold]{r['detection_rate']}[/] "
                 f"({r['detected']}/{r['total']}, {r['wall_s']}s)"
                 + (f"  [yellow]survivors: {r['survivors']}[/]" if r["survivors"] else ""))
        if suite in ("b6", "all"):
            echo("[bold]B6 triage fidelity[/] (does /triage localize the injected bug?)")
            r = run_b6(c.ws.root, work, max_mutants=min(mutants, 3),
                       on_event=lambda m: echo(
                           f"  {m['mutant']}: detected={m['detected']} localized={m['localized']}"))
            reports.append(r)
            echo(f"  localization rate: [bold]{r['localization_rate']}[/]")
    out = out_dir / "report.json"
    _emit_to = json.dumps(reports, indent=2)
    out.write_text(_emit_to)
    echo(f"[dim]report: {c.rel(str(out))}[/]")


@cli.command()
@click.pass_context
def dashboard(ctx):
    """Generate the read-only static HTML dashboard (SPEC §7.5)."""
    from .dashboard import generate_dashboard
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    path = generate_dashboard(c.ws)
    echo(f"[green]dashboard:[/] {c.rel(path)}")


@cli.command()
@click.option("--tag", default="smoke")
@click.pass_context
def ci(ctx, tag):
    """Headless CI gate (SPEC §7.3): lint + tagged sims; nonzero exit on any
    failure; writes a markdown report for the CI system to attach."""
    from .tools import all_tools as _t
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    lines = ["# chipchamp ci report", ""]
    ok = True
    lint = _t()["lint.run"].handler(c)
    lint_ok = lint.get("errors", 1) == 0
    ok &= lint_ok
    lines.append(f"- lint: {'PASS' if lint_ok else 'FAIL'} "
                 f"({lint.get('errors')} errors, {lint.get('warnings')} warnings, "
                 f"job {lint.get('job')})")
    for t in c.ws.tests_with_tag(tag):
        r = _t()["sim.run"].handler(c, test=t["name"], seed=1)
        passed = r.get("sim_status") == "pass"
        ok &= passed
        lines.append(f"- sim {t['name']}: {'PASS' if passed else 'FAIL'} "
                     f"(job {r.get('job')})")
    from .dashboard import generate_dashboard
    generate_dashboard(c.ws)
    report = c.ws.dot / "ci-report.md"
    report.write_text("\n".join(lines) + "\n")
    echo("\n".join(lines))
    echo(f"[dim]report: {c.rel(str(report))} · dashboard regenerated[/]")
    raise SystemExit(0 if ok else 1)


@cli.command()
@click.option("--tag", default="smoke", help="test tag to regress")
@click.option("--seeds", default=3, help="seeds per test")
@click.option("--triage/--no-triage", default=True,
              help="root-cause the failure clusters with the agent")
@click.pass_context
def nightly(ctx, tag, seeds, triage):
    """The unattended run: regress, accumulate flake history, publish.

    The difference between a tool someone opens and infrastructure a team
    depends on. Everything it needs already existed separately — the parallel
    regression, the signature clustering, the history store, the dashboard —
    but nothing composed them into something cron could call."""
    from .jobs.regression import RegressionManager
    from .tools import all_tools as _t
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    tools = _t()
    tests = [t["name"] for t in c.ws.tests_with_tag(tag)]
    if not tests:
        echo(f"[yellow]no tests tagged '{tag}'[/]")
        raise SystemExit(1)
    seed_list = list(range(1, max(1, seeds) + 1))
    echo(f"[bold]{len(tests)}[/] test(s) × {len(seed_list)} seed(s) — tag {tag}")

    def run_one(test: str, seed: int):
        tools["sim.run"].handler(c, test=test, seed=seed)
        return c.task_jobs[-1]

    mgr = RegressionManager(run_one, max_parallel=4)
    res = mgr.run(tests, seeds=seed_list)
    echo(f"\n{res.summary()}")
    # history is what makes tomorrow's run smarter than tonight's: the same
    # seed disagreeing with itself is only visible across runs
    flaky = c.history.flaky_tests()
    if flaky:
        echo(f"\n[bold yellow]{len(flaky)} flaky[/] "
             f"[dim](same seed, different answers — `chipchamp flaky`)[/]")
        for f in flaky[:5]:
            echo(f"  {f['test']} seed {f['seed']} — {f['flake_rate']:.0%} fail "
                 f"over {f['runs']} runs")
    if triage and res.clusters:
        gw, reason, _ = _build_gateway(c)
        if gw is None:
            echo(f"[dim]no model for triage ({reason})[/]")
        else:
            _nightly_triage(c, gw, res)
    from .dashboard import generate_dashboard
    path = generate_dashboard(c.ws)
    echo(f"\n[dim]dashboard: {c.rel(str(path))}[/]")
    raise SystemExit(0 if not res.clusters else 1)


def _nightly_triage(c, gw, res) -> None:
    """One subagent per failure cluster — the fan-out hardware makes obvious
    and nothing could reach until agent.spawn existed."""
    from .agent.orchestrator import Orchestrator, SubagentTask
    echo(f"\n[bold]triaging[/] {len(res.clusters)} cluster(s)")
    orch = Orchestrator(c, lambda role: gw, max_concurrent=3,
                        on_event=_agent_events(c))
    tasks = [SubagentTask(role="triage-analyst", label=cl.id,
                          prompt=f"Root-cause this failure cluster. Signature: "
                                 f"{cl.signature}. Representative job: "
                                 f"{getattr(cl, 'representative', '?')}. "
                                 f"Cite file:line and the mechanism.")
             for cl in res.clusters[:6]]
    for r in orch.run(tasks):
        echo(f"\n[bold]{r.label}[/] [dim]{r.role}[/]")
        echo(f"  {r.error or (r.text or '')[:600]}")


@cli.command()
@click.option("--session", "session_id", default="",
              help="session to wake (default: the most recent parked one)")
@click.option("--timeout", default=3600.0, help="seconds to wait per job")
@click.option("--model", default=None)
@click.option("--provider", default=None)
@click.option("--max-steps", default=20)
@click.pass_context
def wake(ctx, session_id, timeout, model, provider, max_steps):
    """Collect a parked session's detached jobs and continue the task.

    This is the other half of `sim.run(detach=true)`. Detaching alone just
    moves the blocking from the runner to whoever is watching the REPL; a job
    that finishes at 3am also needs something to pick the work back up. Run
    this from cron, from a farm epilogue, or by hand in the morning."""
    from .agent import AgentLoop, NullGateway, Session
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    store = str(c.ws.dot / "sessions")
    sess = (Session.load(session_id, store) if session_id
            else _latest_parked(store))
    if sess is None:
        echo("[dim]no parked session — nothing is waiting on a detached job[/]")
        return
    jobs = sess.task_frame.get("pending_jobs") or []
    if not jobs:
        echo(f"[dim]{sess.id} has no pending jobs[/]")
        return
    echo(f"[bold]{sess.id}[/] parked on {len(jobs)} job(s): {', '.join(jobs)}")
    lines, unfinished = [], []
    for jid in jobs:
        rec = c.runner.wait(jid, timeout=timeout)
        if rec is None or rec.status == "running":
            unfinished.append(jid)
            echo(f"  [yellow]…[/] {jid} still running after {timeout:.0f}s")
            continue
        col = "green" if rec.status == "passed" else "red"
        echo(f"  [{col}]▪[/] {jid} {rec.kind} [{col}]{rec.status}[/] "
             f"[dim]{rec.summary[:70]}[/]")
        lines.append(f"- {jid} ({rec.kind}): {rec.status} — {rec.summary[:200]}")
    # Only the finished ones leave the parking list; an unfinished job stays
    # parked so a later wake still knows to collect it.
    sess.task_frame["pending_jobs"] = unfinished
    sess.save()
    if not lines:
        echo("[yellow]nothing finished[/] — still parked")
        return
    gw, reason, _audit = _build_gateway(c, model, provider)
    if gw is None:
        echo(f"[dim]results recorded; no model to continue with ({reason})[/]")
        return
    task = sess.task_frame.get("parked_task", "")
    events = _agent_events(c)
    loop = AgentLoop(c, gw or NullGateway(), session=sess, max_steps=max_steps,
                     on_event=events, router=_build_router(c),
                     context_budget=_context_budget(c, gw))
    out = loop.run("The detached jobs you launched have finished:\n"
                   + "\n".join(lines)
                   + f"\n\nContinue the task from here: {task}")
    sess.save()
    if out.get("text"):
        echo(out["text"])


def _latest_parked(store: str):
    """The most recent session that is waiting on something."""
    from .agent import Session
    for sid in Session.list_sessions(store):
        s = Session.load(sid, store)
        if s and s.task_frame.get("pending_jobs"):
            return s
    return None


@cli.command()
@click.option("--test", default="", help="one test's history instead of the summary")
@click.pass_context
def flaky(ctx, test):
    """Tests whose own history contradicts itself (SPEC FR-JOB-06).

    A test that fails at seed 3 and passes at seed 4 is not flaky — it found a
    bug at seed 3. Flakiness is disagreement at the SAME seed, which is the
    only place nondeterminism can hide."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    if test:
        runs = c.history.runs(test) or []
        seeds = {k.rpartition("#")[2] for k in c.history._load()
                 if k.rpartition("#")[0] == test}
        if not seeds:
            echo(f"[dim]no recorded runs for {test}[/]")
            return
        for s in sorted(seeds):
            v = c.history.verdict(test, None if s == "-" else int(s))
            col = {"stable": "green", "flaky": "yellow",
                   "failing": "red"}.get(v["verdict"], "grey62")
            echo(f"[bold]{test}[/] seed {s}: [{col}]{v['verdict']}[/] "
                 f"[dim]{v.get('passed', 0)}/{v.get('runs', 0)} passed[/]")
            for sig in v.get("signatures", []):
                echo(f"    [dim]{sig[:90]}[/]")
        return
    summ = c.history.summary()
    if ctx.obj.get("as_json"):
        _emit(summ, True)
        return
    if not summ["tracked"]:
        echo("[dim]no test history yet — it accumulates as sims run[/]")
        return
    echo(f"[bold]{summ['tracked']}[/] (test, seed) pair(s) tracked  [dim]"
         + " · ".join(f"{k} {v}" for k, v in sorted(summ["verdicts"].items()))
         + "[/]")
    if not summ["flaky"]:
        echo("[green]no flaky tests[/] [dim]— every seed agrees with itself[/]")
        return
    echo("\n[bold yellow]flaky[/] [dim](same seed, different answers)[/]")
    for f in summ["flaky"]:
        echo(f"  [bold]{f['test']}[/] [dim]seed {f['seed']}[/] — "
             f"[yellow]{f['flake_rate']:.0%}[/] fail over {f['runs']} runs")
        for sig in f.get("signatures", [])[:2]:
            echo(f"    [dim]{sig[:90]}[/]")


@cli.group("notebook", invoke_without_command=True)
@click.pass_context
def notebook_grp(ctx):
    """What this project has learned, across sessions.

    Root causes, fix patterns, hardware constraints — the things not derivable
    from the tree. A digest goes into every system prompt, which is exactly the
    pressure that keeps it short."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(notebook_list_cmd)


@notebook_grp.command("list")
@click.option("--kind", default="", help="root_cause/fix_pattern/constraint/…")
@click.pass_context
def notebook_list_cmd(ctx, kind):
    """Findings, newest first."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    rows = c.notebook.entries(kind)
    if ctx.obj.get("as_json"):
        _emit(rows, True)
        return
    if not rows:
        echo("[dim]nothing learned yet — the agent records findings with "
             "note.add, or add one with `chipchamp notebook add`[/]")
        return
    colour = {"root_cause": "red", "fix_pattern": "green", "constraint": "yellow",
              "decision": "cyan", "gotcha": "magenta"}
    for e in rows:
        echo(f"[{colour.get(e['kind'], 'white')}]{e['kind']:12}[/] "
             f"[bold]{e['subject']}[/] [dim]{e['id']}[/]")
        echo(f"  {e['detail'][:300]}")
        if e.get("evidence"):
            echo(f"  [dim]evidence: {e['evidence']}[/]")


@notebook_grp.command("add")
@click.argument("kind")
@click.argument("subject")
@click.argument("detail")
@click.option("--evidence", default="")
@click.pass_context
def notebook_add_cmd(ctx, kind, subject, detail, evidence):
    """Record a finding by hand (kind: root_cause/fix_pattern/constraint/…)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    out = c.notebook.add(kind, subject, detail, evidence=evidence, source="human")
    if out.get("error"):
        echo(f"[red]{out['error']}[/]")
        raise SystemExit(1)
    echo(f"[green]recorded[/] {out['id']}")


@notebook_grp.command("rm")
@click.argument("note_id")
@click.pass_context
def notebook_rm_cmd(ctx, note_id):
    """Delete a finding — a wrong one is worse than none."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    if c.notebook.remove(note_id):
        echo(f"[green]removed[/] {note_id}")
    else:
        echo(f"[yellow]no note {note_id}[/]")
        raise SystemExit(1)


@cli.group("experiment", invoke_without_command=True)
@click.pass_context
def experiment_grp(ctx):
    """Recorded agent runs (SPEC §17.2) — the substrate for an autopsy.

    Record one with `chipchamp --experiment <label> -p "<task>"`, then compare
    them here. Nothing is recorded unless asked for: an unlabelled run leaves
    no file, so the store stays a deliberate record rather than a log."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(experiment_list_cmd)


@experiment_grp.command("list")
@click.option("--limit", default=25)
@click.pass_context
def experiment_list_cmd(ctx, limit):
    """Recorded runs, newest first."""
    from .bench.experiment import load_experiments
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    exps = load_experiments(c.ws.dot, limit=limit)
    if not exps:
        echo("[dim]no experiments recorded — "
             "`chipchamp --experiment <label> -p \"<task>\"`[/]")
        return
    if ctx.obj.get("as_json"):
        _emit([e.to_dict() for e in exps], True)
        return
    for e in exps:
        m = e.metrics or {}
        colour = {"completed": "green", "answered": "cyan"}.get(e.outcome, "yellow")
        echo(f"[bold]{e.id}[/]  [{colour}]{e.outcome:9}[/] "
             f"[dim]{e.label or '-'}[/]")
        echo(f"  [dim]{e.model or '?'} · {m.get('steps', 0)} steps · "
             f"{m.get('wall_s', 0):.0f}s · {m.get('tokens_total', 0)} tok · "
             f"{m.get('jobs', 0)} jobs · ctx peak "
             f"{m.get('context_chars_max', 0) // 1000}k chars[/]")


@experiment_grp.command("show")
@click.argument("exp_id")
@click.pass_context
def experiment_show_cmd(ctx, exp_id):
    """Everything recorded about one run."""
    from .bench.experiment import load_experiments
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    hit = next((e for e in load_experiments(c.ws.dot)
                if e.id == exp_id or e.id.endswith(exp_id)), None)
    if hit is None:
        echo(f"[yellow]no experiment '{exp_id}'[/]")
        raise SystemExit(1)
    _emit(hit.to_dict(), True)


@experiment_grp.command("autopsy")
@click.option("--label", default="", help="only runs with this label")
@click.pass_context
def experiment_autopsy_cmd(ctx, label):
    """Compare recorded runs: per-model outcomes, cost, failing tools."""
    from .bench.experiment import autopsy, load_experiments
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    exps = [e for e in load_experiments(c.ws.dot)
            if not label or e.label == label]
    if not exps:
        echo("[dim]nothing to autopsy[/]")
        return
    rep = autopsy(exps)
    if ctx.obj.get("as_json"):
        _emit(rep, True)
        return
    echo(f"[bold]{rep['runs']}[/] run(s)"
         + (f" labelled [bold]{label}[/]" if label else ""))
    for model, m in sorted(rep["by_model"].items(),
                           key=lambda kv: -kv[1]["completion_rate"]):
        outs = " ".join(f"{k}={v}" for k, v in sorted(m["outcomes"].items()))
        echo(f"\n[bold cyan]{model}[/] [dim]n={m['runs']}[/]")
        echo(f"  completed {m['completion_rate']:.0%}  [dim]{outs}[/]")
        echo(f"  [dim]avg {m['avg_steps']} steps · {m['avg_wall_s']:.0f}s · "
             f"{m['avg_tokens']} tok · jobs {m['jobs_passed']}/{m['jobs']} passed"
             f" · {m['switches']} switch(es) · ctx peak "
             f"{m['context_chars_max'] // 1000}k chars[/]")
    if rep["tool_failures"]:
        echo("\n[bold]tools that failed most[/]")
        for name, n in rep["tool_failures"].items():
            echo(f"  [yellow]{n:3}[/] {name}")


@cli.command()
@click.pass_context
def packs(ctx):
    """List available knowledge packs (protocol/methodology collateral)."""
    from .packs import list_packs
    for p in list_packs(ctx.obj["root"]):
        echo(f"[bold]{p['name']}[/] — {p['summary']}")
        echo(f"  [dim]{p['path']} · {len(p['files'])} file(s)[/]")


@cli.group("skills", invoke_without_command=True)
@click.pass_context
def skills_grp(ctx):
    """Manage skill directories (expert playbooks, SKILL.md format)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(skills_list_cmd)


def _skills_ws(ctx):
    return _ctx(ctx.obj["root"], ctx.obj["target"]).ws


@skills_grp.command("add")
@click.argument("directory")
@click.pass_context
def skills_add_cmd(ctx, directory):
    """Point a directory containing skills (each: <name>/SKILL.md)."""
    import os as _os

    from .skills import add_registry_dir, discover_report
    ws = _skills_ws(ctx)
    d = _os.path.abspath(_os.path.expanduser(directory))
    if not _os.path.isdir(d):
        echo(f"[red]not a directory: {directory}[/]")
        raise SystemExit(1)
    add_registry_dir(ws, d)
    rep = discover_report(ws)
    mine = [s for s in rep.skills.values() if str(s.dir).startswith(d)]
    shadowed = [w for w in rep.shadowed if f"({d}" in w]
    echo(f"[green]registered[/] {d} — {len(mine)} skill(s) found"
         + (f" [yellow]({len(shadowed)} shadowed by earlier dirs)[/]"
            if shadowed else ""))
    for w in shadowed[:5]:
        echo(f"  [yellow]{w}[/]")


@skills_grp.command("list")
@click.pass_context
def skills_list_cmd(ctx):
    """Show pointed directories and every discovered skill."""
    from .skills import discover_report, first_sentence, skill_dirs
    ws = _skills_ws(ctx)
    rep = discover_report(ws)
    if ctx.obj["as_json"]:
        _emit({"dirs": [{"path": str(p), "source": src}
                        for p, src in skill_dirs(ws)],
               "skills": [{"name": s.name, "description": s.description,
                           "dir": str(s.dir), "source": s.source}
                          for s in rep.skills.values()],
               "shadowed": rep.shadowed, "warnings": rep.warnings}, True)
        return
    dirs = skill_dirs(ws)
    if not dirs:
        echo("[dim]no skill directories — add one with[/] "
             "[bold]chipchamp skills add <dir>[/]")
        return
    echo("[bold]directories[/]")
    for p, src in dirs:
        echo(f"  {p}  [dim]({src})[/]")
    echo(f"[bold]skills[/] ({len(rep.skills)})")
    for s in rep.skills.values():
        echo(f"  [bold]{s.name:24}[/] {first_sentence(s.description)[:72]}")
    for w in rep.shadowed:
        echo(f"[yellow]shadowed: {w}[/]")
    for w in rep.warnings:
        echo(f"[yellow]warning: {w}[/]")


@skills_grp.command("remove")
@click.argument("directory", required=False)
@click.option("--all", "remove_all", is_flag=True, help="remove every pointer")
@click.pass_context
def skills_remove_cmd(ctx, directory, remove_all):
    """Remove a pointed directory (or all of them)."""
    from .skills import load_registry, remove_registry_dir
    ws = _skills_ws(ctx)
    if remove_all:
        remove_registry_dir(ws, all=True)
        echo("[green]cleared all registered skill directories[/]")
        return
    if not directory:
        echo("[red]give a directory, or --all[/]")
        raise SystemExit(1)
    import os as _os
    d = _os.path.abspath(_os.path.expanduser(directory))
    before = load_registry(ws)
    after = remove_registry_dir(ws, d)
    if len(after) < len(before):
        echo(f"[green]removed[/] {d}")
    else:
        echo(f"[yellow]{d} was not registered[/] — `chipchamp skills list` "
             f"shows sources (workspace/config dirs aren't registry-managed)")


@skills_grp.command("reindex")
@click.pass_context
def skills_reindex_cmd(ctx):
    """(Re)build the semantic-trigger embedding cache for every skill."""
    from .skills import discover
    from .skills_semantic import cache_path, corpus_vectors, resolve_endpoint
    ws = _skills_ws(ctx)
    sk = discover(ws)
    if not sk:
        echo("[dim]no skills to index — add a directory first[/]")
        return
    try:
        base, model = resolve_endpoint(ws)
        vecs = corpus_vectors(ws, sk)
    except Exception as e:
        echo(f"[red]semantic index unavailable:[/] {e}")
        echo("[dim]the agent falls back to the static compact index; set "
             "[skills] embed_provider/embed_model in config.toml to pin an "
             "endpoint[/]")
        raise SystemExit(1)
    echo(f"[green]indexed[/] {len(vecs)} skill(s) with [bold]{model}[/] at "
         f"{base} → {ws.rel(str(cache_path(ws)))}")


@skills_grp.command("show")
@click.argument("name")
@click.pass_context
def skills_show_cmd(ctx, name):
    """Print one skill's playbook."""
    from .skills import discover, skill_body
    ws = _skills_ws(ctx)
    sk = discover(ws)
    s = sk.get(name)
    if s is None:
        import difflib
        close = difflib.get_close_matches(name, list(sk), n=3, cutoff=0.5)
        echo(f"[red]unknown skill '{name}'[/]"
             + (f" — did you mean: {', '.join(close)}?" if close else ""))
        raise SystemExit(1)
    body = skill_body(s)
    if ctx.obj["as_json"]:
        _emit(body, True)
        return
    echo(f"[dim]{s.dir}  ·  source: {s.source}"
         + (f"  ·  files: {', '.join(body['files'])}" if body["files"] else "")
         + "[/]")
    if _con:
        from . import ui
        ui.markdown(body["body"])
    else:
        echo(body["body"])


@cli.group("library", invoke_without_command=True)
@click.pass_context
def library_grp(ctx):
    """Manage IP library directories (manifest.json component libraries)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(library_list_cmd)


@library_grp.command("add")
@click.argument("directory")
@click.pass_context
def library_add_cmd(ctx, directory):
    """Point a library root (a directory carrying manifest.json)."""
    import os as _os

    from .library import add_registry_dir, discover_report
    ws = _skills_ws(ctx)
    d = _os.path.abspath(_os.path.expanduser(directory))
    if not _os.path.isdir(d):
        echo(f"[red]not a directory: {directory}[/]")
        raise SystemExit(1)
    if not _os.path.isfile(_os.path.join(d, "manifest.json")):
        echo(f"[red]{directory} has no manifest.json — not a component library[/]")
        raise SystemExit(1)
    add_registry_dir(ws, d)
    rep = discover_report(ws)
    mine = [m for m in rep.modules.values() if str(m.root) == d]
    echo(f"[green]registered[/] {d} — {len(mine)} component(s) found")
    for w in rep.warnings[:5]:
        echo(f"  [yellow]{w}[/]")


@library_grp.command("list")
@click.option("--category", default="", help="filter by category prefix")
@click.pass_context
def library_list_cmd(ctx, category=""):
    """Show registered libraries and their components."""
    from .library import discover_report, library_dirs
    ws = _skills_ws(ctx)
    rep = discover_report(ws)
    if ctx.obj["as_json"]:
        _emit({"dirs": [{"path": str(p), "source": src}
                        for p, src in library_dirs(ws)],
               "libraries": rep.libraries,
               "components": [{"name": m.name, "category": m.category,
                               "summary": m.summary, "dir": str(m.dir),
                               "status": m.meta.get("status", "")}
                              for m in rep.modules.values()
                              if not category
                              or m.category.startswith(category)],
               "shadowed": rep.shadowed, "warnings": rep.warnings}, True)
        return
    if not rep.libraries:
        echo("[dim]no IP libraries — add one with[/] "
             "[bold]chipchamp library add <dir>[/]")
        return
    for lib in rep.libraries:
        echo(f"[bold]{lib['name']}[/]  {lib['root']}  "
             f"[dim]{lib['count']} component(s) ({lib['source']})[/]")
    cur = None
    for m in sorted(rep.modules.values(), key=lambda x: (x.category, x.name)):
        if category and not m.category.startswith(category):
            continue
        if m.category != cur:
            cur = m.category
            echo(f"  [bold]{cur}[/]")
        echo(f"    {m.name:24} {m.summary[:66]}")
    for w in rep.warnings:
        echo(f"[yellow]warning: {w}[/]")


@library_grp.command("remove")
@click.argument("directory", required=False)
@click.option("--all", "remove_all", is_flag=True, help="remove every pointer")
@click.pass_context
def library_remove_cmd(ctx, directory, remove_all):
    """Remove a pointed library (or all of them)."""
    from .library import load_registry, remove_registry_dir
    ws = _skills_ws(ctx)
    if remove_all:
        remove_registry_dir(ws, all=True)
        echo("[green]cleared all registered library directories[/]")
        return
    if not directory:
        echo("[red]give a directory, or --all[/]")
        raise SystemExit(1)
    import os as _os
    d = _os.path.abspath(_os.path.expanduser(directory))
    before = load_registry(ws)
    after = remove_registry_dir(ws, d)
    if len(after) < len(before):
        echo(f"[green]removed[/] {d}")
    else:
        echo(f"[yellow]{d} was not registered[/] — config-declared dirs "
             f"aren't registry-managed")


@library_grp.command("show")
@click.argument("name")
@click.pass_context
def library_show_cmd(ctx, name):
    """Print one component's card (params/ports + instantiation template)."""
    from .library import discover, module_card
    ws = _skills_ws(ctx)
    mods = discover(ws)
    m = mods.get(name)
    if m is None:
        import difflib
        close = difflib.get_close_matches(name, list(mods), n=3, cutoff=0.5)
        echo(f"[red]unknown component '{name}'[/]"
             + (f" — did you mean: {', '.join(close)}?" if close else ""))
        raise SystemExit(1)
    card = module_card(m)
    if ctx.obj["as_json"]:
        _emit(card, True)
        return
    echo(f"[bold]{card['name']}[/]  [{card['category']}]  "
         f"v{card['version']}  [dim]{card['status']} · {card['dir']}[/]")
    echo(card["summary"])
    if card["params"]:
        echo("[bold]params[/]")
        for p in card["params"]:
            echo(f"  {p.get('name', ''):16} default={p.get('default', '')} "
                 f"[dim]{p.get('desc', '')}[/]")
    echo("[bold]instantiation[/]")
    echo(card["instantiation"])


@library_grp.command("search")
@click.argument("query")
@click.option("--category", default="")
@click.pass_context
def library_search_cmd(ctx, query, category):
    """Keyword search over the component manifests."""
    from .library import search
    ws = _skills_ws(ctx)
    hits = search(ws, query=query, category=category)
    if ctx.obj["as_json"]:
        _emit([{"name": m.name, "category": m.category, "summary": m.summary}
               for m in hits], True)
        return
    if not hits:
        echo("[dim]no match — `chipchamp library list` shows the category map[/]")
        return
    for m in hits:
        echo(f"  [bold]{m.name:24}[/] [{m.category}] {m.summary[:56]}")


@library_grp.command("match")
@click.argument("behavior")
@click.option("--interface", default="", help="protocol/ports/handshake/clocks hint")
@click.option("--category", default="")
@click.pass_context
def library_match_cmd(ctx, behavior, interface, category):
    """Match components by function + interface (name-independent)."""
    from .library_semantic import match
    ws = _skills_ws(ctx)
    try:
        hits = match(ws, behavior, interface=interface, category=category)
    except Exception as e:
        echo(f"[red]match unavailable:[/] {e}")
        raise SystemExit(1)
    if ctx.obj["as_json"]:
        _emit([{"name": m.name, "category": m.category, "score": round(sc, 3),
                "semantic": info["semantic"], "interface": info["interface_signature"],
                "why": info["why"]} for m, sc, info in hits], True)
        return
    if not hits:
        echo("[dim]no components — is a library registered?[/]")
        return
    for m, sc, info in hits:
        why = ", ".join(info["why"]) or "functional similarity"
        echo(f"  [bold]{m.name:22}[/] [dim]{sc:.2f}[/] [{m.category}] "
             f"[dim]{m.summary[:50]}[/]")
        echo(f"      [dim]{info['interface_signature'][:100]}[/]")
        echo(f"      [cyan]why:[/] {why}")


@library_grp.command("reindex")
@click.pass_context
def library_reindex_cmd(ctx):
    """(Re)build the semantic-match embedding cache for every component."""
    from .library import discover
    from .library_semantic import cache_path, component_vectors
    from .embeddings import resolve_endpoint
    ws = _skills_ws(ctx)
    mods = discover(ws)
    if not mods:
        echo("[dim]no components — register a library with `library add <dir>`[/]")
        return
    try:
        cfg = ws.library_cfg
        base, model = resolve_endpoint(ws, str(cfg.get("embed_provider", "")).strip(),
                                       str(cfg.get("embed_model", "")).strip())
        vecs = component_vectors(ws, mods)
    except Exception as e:
        echo(f"[red]semantic match unavailable:[/] {e}")
        echo("[dim]lib.match falls back to lexical + interface scoring; set "
             "[library] embed_provider/embed_model to pin an endpoint[/]")
        raise SystemExit(1)
    echo(f"[green]indexed[/] {len(vecs)} component(s) with [bold]{model}[/] at "
         f"{base} → {ws.rel(str(cache_path(ws)))}")


@library_grp.command("fetch")
@click.argument("name")
@click.option("--view", type=click.Choice(["rtl", "sim"]), default="rtl",
              help="rtl = synthesizable fileset; sim = C++ golden ref model")
@click.option("--dest", default="", help="workspace-relative destination")
@click.pass_context
def library_fetch_cmd(ctx, name, view, dest):
    """Copy a component into the workspace (rtl fileset or C++ ref model)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    res = all_tools()["lib.fetch"].handler(c, name=name, view=view, dest=dest)
    if ctx.obj["as_json"]:
        _emit(res, True)
        return
    if "error" in res:
        echo(f"[red]{res['error']}[/]")
        raise SystemExit(1)
    for f in res["written"]:
        echo(f"  [green]fetched[/] {f}")
    for f in res["unchanged"]:
        echo(f"  [dim]unchanged {f}[/]")
    for d in res.get("denied", []):
        echo(f"  [red]denied {d['path']}: {d['reason']}[/]")
    echo(f"[dim]{res['note']}[/]")
    if "instantiation" not in res:
        return
    echo("[bold]instantiation[/]")
    echo(res["instantiation"])


@cli.command()
@click.argument("path", required=False)
@click.option("--unpin", is_flag=True, help="clear the pinned default working repo")
@click.pass_context
def workspace(ctx, path, unpin):
    """Show the working repo, or pin/clear a default one.

    `chipchamp workspace`            → show the resolved repo and how it was chosen
    `chipchamp workspace <path>`     → pin <path> as the default (used from anywhere)
    `chipchamp workspace .`          → pin the current directory
    `chipchamp workspace --unpin`    → clear the pinned default
    """
    from .config import find_project_root, pin_root, pinned_root, resolve_root, unpin_root
    if unpin:
        echo("[green]unpinned[/]" if unpin_root() else "[dim]no pinned default[/]")
        return
    if path is not None:
        pinned = pin_root(path)
        echo(f"[green]pinned default working repo:[/] {pinned}")
        return
    root = ctx.obj["root"]
    how = ctx.obj.get("root_how", "")
    echo(f"[bold]working repo:[/] {root}  [dim]({how})[/]")
    echo(f"[dim]cwd:[/] {os.getcwd()}")
    echo(f"[dim]git/project root of cwd:[/] {find_project_root(os.getcwd())}")
    pin = pinned_root()
    echo(f"[dim]pinned default:[/] {pin or '(none)'}")
    echo(f"[dim]precedence: --root > {_brand.env_name('ROOT')} > pinned > auto-detect[/]")


@cli.command("mcp-serve")
@click.pass_context
def mcp_serve(ctx):
    """Serve the design DB / jobs / waves as a read-only MCP server on stdio."""
    from .mcp import McpServer
    McpServer(ctx.obj["root"], ctx.obj["target"]).serve()


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
