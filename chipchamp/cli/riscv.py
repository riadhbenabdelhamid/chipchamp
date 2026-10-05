"""chipchamp CLI — riscv commands (split from the one-file cli.py)."""
from __future__ import annotations


import click


try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _ctx, _emit, _run_tool, cli


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
    r = _run_tool(c, "riscv.toolchain")
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
    echo("  reference     " + (f"[green]{model}[/] {r.get('reference_exe','')}"
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
    r = _run_tool(c, "riscv.compile", sources=list(sources), name=name, march=march, mabi=mabi,
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
    r = _run_tool(c, "riscv.refrun", elf=elf, max_steps=max_steps,
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
    r = _run_tool(c, "riscv.cosim", core_trace=core_trace, elf=elf, reference_trace=reference_trace,
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
    r = _run_tool(c, "riscv.compliance", config=config, arch_test=arch_test, suite=suite, env=env_dir,
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
