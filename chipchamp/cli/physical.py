"""chipchamp CLI — physical commands (split from the one-file cli.py)."""
from __future__ import annotations


import click

from ..tools import all_tools

try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _ctx, _emit, _run_tool, cli


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
    r = _run_tool(c, "efpga-fabulous.fabric", project=project)
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
    r = _run_tool(c, "efpga-fabulous.bitstream", design=design, project=project, with_fabric=with_fabric)
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
    r = _run_tool(c, "pd.run", module=module, clock_period=clock_period,
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
    from ..agent.playbooks import run_optimize
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
    r = _run_tool(c, "fpga.run", module=module,
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
        b = _run_tool(c, "fpga.bitstream", module=module, job=r["job"])
        if "error" in b:
            echo(f"  [red]bitstream: {b['error']}[/]"
                 + (f"\n    [dim]{b['hint']}[/]" if b.get("hint") else ""))
        else:
            echo(f"  [green]bitstream[/] {b['bitstream']} "
                 f"[dim]({b['bitstream_bytes']} bytes via {b['packer']})[/]")
    ppa = _run_tool(c, "fpga.ppa", module=module, job=r["job"],
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
    crit = _run_tool(c, "fpga.critical", module=module, job=r["job"], n=1)
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


# ---- RapidWright: headless checkpoint analysis ----------------------------

@cli.group("dcp", invoke_without_command=True)
@click.pass_context
def dcp_grp(ctx):
    """Headless analysis of a vendor design checkpoint (RapidWright)."""
    if ctx.invoked_subcommand is None:
        echo(ctx.get_help())


@dcp_grp.command("stats")
@click.option("--module", default="", help="defaults to the latest fpga run")
@click.option("--job", default="", help="a specific fpga.run job id")
@click.option("--dcp", default="", help="workspace-relative .dcp path")
@click.pass_context
def dcp_stats_cmd(ctx, module, job, dcp):
    """Cells, sites, clock regions and routing summary of a checkpoint."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _emit(_run_tool(c, "fpga.stats", module=module, job=job, dcp=dcp), ctx.obj["as_json"])


@dcp_grp.command("placement")
@click.option("--module", default="")
@click.option("--job", default="")
@click.option("--dcp", default="", help="workspace-relative .dcp path")
@click.option("--highlight", "highlights", multiple=True,
              help="MATCH[=#rrggbb] — cells whose name matches, drawn in colour")
@click.pass_context
def dcp_placement_cmd(ctx, module, job, dcp, highlights):
    """Draw where the design landed on the device (PNG, no GUI)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    hl = []
    for h in highlights:
        m, _, color = h.partition("=")
        hl.append({"match": m, "color": color or "#ffff00", "label": m})
    _emit(_run_tool(c, "fpga.placement", module=module, job=job, dcp=dcp,
                    highlights=hl or None), ctx.obj["as_json"])


@dcp_grp.command("interchange")
@click.option("--module", default="")
@click.option("--job", default="")
@click.option("--dcp", default="", help="workspace-relative .dcp path")
@click.pass_context
def dcp_interchange_cmd(ctx, module, job, dcp):
    """Export the checkpoint to the FPGA Interchange Format."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _emit(_run_tool(c, "fpga.interchange", module=module, job=job, dcp=dcp), ctx.obj["as_json"])
