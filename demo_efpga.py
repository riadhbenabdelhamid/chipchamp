#!/usr/bin/env python
"""eFPGA demo — an FPGA from thin air, then a fresh design programmed onto it.

    python demo_efpga.py            # 1. SCRIPTED  — the platform works
    python demo_efpga.py --agent    # 2. AGENTIC   — the model steers (qwen)

One session spans the whole stack: FABulous generates an FPGA *as RTL* (the
fabric), a brand-new user design is written on the spot, and yosys → nextpnr →
bit_gen turn it into a routed bitstream for that fabric — with the rung-E gates
(`fabric_generated`, `bitstream_generated`) closing through a real
``report.done``. Nothing is stubbed in either mode; the only difference is who
chooses each step, and both modes say which on screen, twice, because a viewer
cannot infer it from the output.

The user design is written DURING the demo rather than shipped with it — that
edit is what classifies the task ``efpga-fabulous`` and puts the E gates on
the ladder, and it makes the story honest: the bitstream proves a design that
did not exist a minute earlier now runs on an FPGA that did not exist either.
"""
from __future__ import annotations

import argparse
import os
import sys

# FABulous + oss-cad-suite must be on PATH before the adapter probes for them.
_VENV_BIN = os.path.dirname(os.path.abspath(sys.executable))
_OSS = os.path.expanduser("~/oss-cad-suite/bin")
os.environ["PATH"] = os.pathsep.join(
    [_VENV_BIN] + ([_OSS] if os.path.isdir(_OSS) else [])
    + [os.environ.get("PATH", "")])

from chipchamp import ui                                   # noqa: E402
from chipchamp.config import Workspace                     # noqa: E402
from chipchamp.tools import all_tools                      # noqa: E402
from chipchamp.tools.context import ToolContext            # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "soc")
PROJECT = os.path.join(ROOT, "efpga-fabulous")
T = all_tools()
C = ui.console()

# A 16-bit maximal-length Fibonacci LFSR (taps 16,15,13,4) on the FABulous
# demo-fabric user-design interface. Same port contract as the shipped
# sequential_16bit_en.v; genuinely different behaviour (a PRBS, not a counter).
LFSR_DESIGN = """\
`default_nettype none

// Written by the chipchamp eFPGA demo: a fresh design for a fresh fabric.
module lfsr16 (
    input  wire        clk,
    input  wire [27:0] io_in,
    output wire [27:0] io_out,
    output wire [27:0] io_oeb
);
    wire rst = io_in[0];
    wire en  = io_in[1];
    reg [15:0] r;

    wire fb = r[15] ^ r[14] ^ r[12] ^ r[3];   // x^16+x^15+x^13+x^4+1

    always @(posedge clk)
        if (rst)      r <= 16'hACE1;          // any non-zero seed
        else if (en)  r <= {r[14:0], fb};

    assign io_out = {12'b0, r};
    assign io_oeb = 28'b0000000000000000000000000001;
endmodule
`default_nettype wire
"""
DESIGN_REL = "user_design/lfsr16.v"          # relative to the FABulous project


# ---- framing (same contract as demo_waveform.py) ----------------------------

def banner(agent: bool, model: str = "") -> None:
    if agent:
        C.print("\n[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                "[bold]a model chooses every step[/]")
        C.print("[dim]  Shows AUTONOMY: the model generates the fabric, writes "
                "a new user design itself, programs it, and closes the gates.[/]")
        C.print(f"[dim]  Driven by [/][bold]{model}[/][dim]. The outcome depends "
                f"on that model's ability to steer and call tools — it may "
                f"wander or fail. That is part of what is being shown.[/]")
    else:
        C.print("\n[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[bold]every step below is hardcoded[/]")
        C.print("[dim]  Shows the PLATFORM: a real FABulous fabric, a real "
                "yosys→nextpnr→bit_gen bitstream, real gates, a real "
                "report.done.[/]")
        C.print("[dim]  It does NOT show agent autonomy — this file chose the "
                "steps, not a model. Run with --agent for that.[/]")
    C.print()


def footer(agent: bool, closed: bool) -> None:
    C.rule()
    if agent:
        C.print("[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                f"[dim]a model chose those steps — it "
                f"{'closed the task' if closed else 'did not close the task'}.[/]")
        C.print("[dim]  Re-running may go differently: that variance IS the "
                "capability being demonstrated.[/]")
    else:
        C.print("[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[dim]those steps were hardcoded. The fabric, bitstream and "
                "gates were real; the decisions were not.[/]")
        C.print("[dim]  For the agent doing it itself: "
                "python demo_efpga.py --agent[/]")


def scene(n: str, title: str) -> None:
    C.print(f"\n[bold cyan]{n}[/] [bold]{title}[/]")
    C.print("[grey30]" + "─" * 74 + "[/]")


def ladder(ctx) -> None:
    rep = ctx.gate_status()
    if rep is not None:
        C.print("  " + ui.gate_ladder(rep))


def ensure_project() -> bool:
    """The FABulous project (fabric spec + demo user design). Created once;
    generation artifacts land inside it, so it is per-machine state, not
    something the repo ships."""
    if os.path.isfile(os.path.join(PROJECT, "fabric.csv")):
        return True
    import subprocess
    C.print(f"[dim]  creating FABulous project at "
            f"{os.path.relpath(PROJECT, ROOT)}…[/]")
    r = subprocess.run(["FABulous", "create-project", PROJECT],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.isfile(os.path.join(PROJECT, "fabric.csv")):
        C.print(f"[red]could not create the FABulous project[/]: "
                f"{(r.stderr or r.stdout)[-200:]}")
        return False
    return True


# ---- mode 1: scripted -------------------------------------------------------

def scripted(ctx) -> bool:
    scene("1 ·", "Generate an FPGA — the fabric itself, as RTL")
    r = T["efpga-fabulous.fabric"].handler(ctx)
    if r.get("status") != "passed":
        C.print(f"  [red]✗[/] fabric generation failed: {r.get('summary')}")
        return False
    C.print(f"  [green]✓[/] [bold]{r['job']}[/] {r['summary']}")
    top = os.path.join(PROJECT, "Fabric", "eFPGA.v")
    tiles = sorted(os.listdir(os.path.join(PROJECT, "Tile")))[:5]
    C.print(f"  [dim]top: Fabric/eFPGA.v ({os.path.getsize(top):,} bytes) · "
            f"tiles: {', '.join(tiles)}…[/]")

    scene("2 ·", "Write a NEW design for it — this creates the obligation")
    C.print("[dim]  A 16-bit maximal LFSR, written now — it did not exist a "
            "minute ago. The edit is what classifies this task "
            "efpga-fabulous and puts the rung-E gates on the ladder:[/]")
    w = T["fs.write"].handler(ctx, path=os.path.join(
        os.path.relpath(PROJECT, ROOT), DESIGN_REL), content=LFSR_DESIGN)
    C.print(f"  [green]✓[/] wrote {w.get('path')} ({w.get('bytes')} B)")
    # The wrapper (pad ring + global clock) instantiates the user design BY
    # MODULE NAME — pointing it at the new design is the genuine FABulous
    # workflow, and skipping it fails synthesis on an unresolved instance.
    wrap_rel = os.path.join(os.path.relpath(PROJECT, ROOT),
                            "user_design", "top_wrapper.v")
    T["fs.edit"].handler(ctx, path=wrap_rel,
                         old="sequential_16bit_en top_i (",
                         new="lfsr16 top_i (")
    C.print(f"  [green]✓[/] retargeted top_wrapper.v at [bold]lfsr16[/]")
    ladder(ctx)

    scene("3 ·", "Program the fabric with it — synth, place, route, bitstream")
    b = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN_REL)
    ok = b.get("status") == "passed" and b.get("bitstream_bytes", 0) > 0
    C.print(f"  [{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] "
            f"[bold]{b.get('job')}[/] {b.get('summary')}")
    if not ok:
        return False
    info = T["efpga-fabulous.info"].handler(ctx)
    C.print(f"  [dim]bitstream {info.get('bitstream_bytes'):,} B · routed "
            f"{info.get('routed')} · fmax {info.get('fmax_mhz')} MHz "
            f"(constraint {info.get('fmax_constraint_mhz')} MHz, "
            f"met={info.get('fmax_met')})[/]")
    ladder(ctx)

    scene("4 ·", "Close it — gates decide, not the narrator")
    done = T["report.done"].handler(
        ctx, summary="lfsr16 written and programmed onto the generated fabric")
    for g in done["gate_table"]:
        mark = {"pass": "[green]✓[/]", "fail": "[red]✗[/]",
                "missing": "[yellow]·[/]"}.get(g["status"], "?")
        C.print(f"    {mark} {g['name']:22} [dim]{g['detail'][:46]}[/]")
    verdict = "ACCEPTED" if done["accepted"] else "REJECTED"
    C.print(f"\n  [bold]report.done[/] ({done['task_class']}): "
            f"[{'green' if done['accepted'] else 'red'}]{verdict}[/]")
    return bool(done["accepted"])


# ---- mode 2: agentic --------------------------------------------------------

PROMPT = (
    "This workspace contains a FABulous eFPGA project at `efpga-fabulous/` "
    "(relative to the workspace root).\n\n"
    "1. Generate the eFPGA fabric.\n"
    "2. Write a NEW user design: a 16-bit maximal-length Fibonacci LFSR "
    "(taps 16,15,13,4). It must use exactly the port interface of the "
    "existing `efpga-fabulous/user_design/sequential_16bit_en.v` — read that "
    "file first. Save yours as `efpga-fabulous/user_design/<name>.v`.\n"
    "   Then update `efpga-fabulous/user_design/top_wrapper.v`: it "
    "instantiates the user design by module name (`sequential_16bit_en "
    "top_i`), and it must instantiate YOURS instead.\n"
    "3. Produce a routed bitstream for YOUR design on the generated fabric "
    "(the design path you pass is relative to the FABulous project, e.g. "
    "`user_design/<name>.v`).\n"
    "4. Verify the result (bitstream bytes, routed, fmax), then close the "
    "task properly."
)


def agentic(ctx, model: str, max_steps: int) -> bool:
    from chipchamp.agent import AgentLoop, Session
    from chipchamp.agent.working_set import budget_for
    from chipchamp.bench.experiment import ExperimentRecorder
    from chipchamp.cli import _agent_events, _build_gateway

    prov, _, mdl = (model or "").partition(":")
    gw, reason, _audit = _build_gateway(ctx, mdl or None, prov or None)
    if gw is None:
        C.print(f"[red]no model available[/] — {reason}")
        return False
    local = gw.ref.startswith(("ollama:", "lmstudio:", "vllm:", "llamacpp:"))
    if local:
        gw.options = {**(gw.options or {}), "num_ctx": 32768}

    scene("0 ·", "Hand the whole flow to the model")
    C.print(f"  [cyan]»[/] {PROMPT.splitlines()[0]}")
    before = set(os.listdir(os.path.join(PROJECT, "user_design")))

    sess = Session.new(str(ctx.ws.dot / "sessions"))
    loop = AgentLoop(ctx, gw, session=sess, max_steps=max_steps,
                     on_event=_agent_events(ctx), disclose=local,
                     context_budget=budget_for(gw, ctx.ws.config))
    rec = ExperimentRecorder(ctx.ws.dot, label="demo-efpga-agentic",
                             task=PROMPT, model=gw.ref)
    rec.attach(loop)
    out = loop.run(PROMPT)
    exp = rec.finish(out, max_steps=max_steps)

    scene("·", "What the model actually did")
    m = exp.metrics
    C.print(f"  model [bold]{exp.model}[/] · steps [bold]{m['steps']}[/] · "
            f"tool calls [bold]{m['tool_calls']}[/] "
            f"([red]{m['tool_failures']} failed[/]) · jobs [bold]{m['jobs']}[/]"
            f" · {m['wall_s']:.0f}s")
    for name, st in sorted((m.get("tools") or {}).items()):
        C.print(f"    [dim]·[/] {name} ×{st['calls']}"
                + (f" [red]({st['failed']} failed)[/]" if st["failed"] else ""))
    ladder(ctx)

    efpga_jobs = [j for j in ctx.task_jobs if j.kind == "efpga-fabulous"]
    fabric_ok = any((j.result.get("metrics") or {}).get("fabric_generated")
                    or (j.result.get("metrics") or {}).get("bitstream_bytes", 0) > 0
                    for j in efpga_jobs)
    wrote = sorted(p for p in ctx.edits
                   if "user_design" in p and not p.endswith("top_wrapper.v"))
    bits = max((int((j.result.get("metrics") or {}).get("bitstream_bytes") or 0)
                for j in efpga_jobs), default=0)
    routed = any((j.result.get("metrics") or {}).get("routed")
                 for j in efpga_jobs)
    closed = bool(m.get("reported_done"))

    def mark(ok):
        return "[green]✓[/]" if ok else "[red]✗[/]"

    C.print()
    C.print(f"  {mark(m['steps'] > 0)} took any action at all")
    C.print(f"  {mark(fabric_ok)} generated the fabric")
    C.print(f"  {mark(bool(wrote))} wrote a user design"
            + (f" [dim]({', '.join(wrote)})[/]" if wrote else ""))
    C.print(f"  {mark(bits > 0 and routed)} produced a routed bitstream"
            + (f" [dim]({bits:,} B)[/]" if bits else ""))
    C.print(f"  {mark(closed)} closed it with an accepted report.done")
    C.print(f"\n  [dim]recorded as experiment[/] [bold]{exp.id}[/] "
            f"[dim](outcome: {exp.outcome})[/]")
    if out.get("text"):
        C.print()
        ui.markdown(out["text"][:700])
    # anything the model added under user_design/ is cleaned by main()'s
    # finally via the `before` snapshot returned here
    agentic.new_files = set(os.listdir(
        os.path.join(PROJECT, "user_design"))) - before
    return closed


# ---- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--model", default="ollama:qwen3.6:35b",
                    help="agentic driver (default: the model that closed the "
                         "waveform demo)")
    ap.add_argument("--max-steps", type=int, default=32)
    ap.add_argument("--keep", action="store_true",
                    help="keep the written user design(s) instead of cleaning")
    a = ap.parse_args()

    from chipchamp.adapters.fabulous import FabulousAdapter
    if not FabulousAdapter().available():
        C.print("[red]FABulous is not on PATH[/] — the eFPGA vertical needs it. "
                "On this machine the working install is reached via the shims "
                "in .venv/bin (FABulous, bit_gen, task).")
        return 2
    if not ensure_project():
        return 2

    ws = Workspace(ROOT)
    ctx = ToolContext(ws)
    wrapper = os.path.join(PROJECT, "user_design", "top_wrapper.v")
    wrapper_src = open(wrapper).read()
    banner(a.agent, a.model)
    closed = False
    try:
        closed = agentic(ctx, a.model, a.max_steps) if a.agent else scripted(ctx)
    finally:
        # remove what the demo (or the model) wrote, so reruns start clean —
        # the fabric and its generation artifacts stay: they are per-machine
        # build products, and regenerating them is 6 seconds if ever wanted
        if not a.keep:
            with open(wrapper, "w") as fh:      # un-retarget the pad ring
                fh.write(wrapper_src)
            victims = {DESIGN_REL.split("/")[-1]} \
                | getattr(agentic, "new_files", set())
            for name in victims:
                p = os.path.join(PROJECT, "user_design", name)
                base = os.path.splitext(p)[0]
                # FABulous emits sibling collateral next to a design (.vh,
                # .vhd, .csv) — sweep the whole family or reruns inherit it
                for ext in (".v", ".vh", ".vhd", ".csv", ".bin", ".fasm",
                            "_npnr_log.txt", ".json"):
                    q = base + ext if not name.endswith(ext) else p
                    if os.path.exists(q):
                        os.unlink(q)
        footer(a.agent, closed)
    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(main())
