#!/usr/bin/env python
"""eFPGA equivalence demo (D) — the bitstream behaves like the RTL, visibly.

    python demo_efpga_equiv.py            # 1. SCRIPTED  — the platform works
    python demo_efpga_equiv.py --agent    # 2. AGENTIC   — the model steers

Demo A ends at "it routes". This one ends at "it WORKS": the generated
fabric netlist is simulated with the bitstream loaded into its config
chain, next to the raw user design running as a cycle-by-cycle gold model —
the FABulous template testbench's own structure (`!==` compare, $fatal on
divergence). A passing cosim is behavioral bitstream-vs-RTL equivalence,
and the demo renders the two waveforms side by side so the match is
something you can SEE, not just a verdict line.

Both modes say on screen which they are, twice, because a viewer cannot
infer it from the output.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

_VENV_BIN = os.path.dirname(os.path.abspath(sys.executable))
_OSS = os.path.expanduser("~/oss-cad-suite/bin")
os.environ["PATH"] = os.pathsep.join(
    [_VENV_BIN] + ([_OSS] if os.path.isdir(_OSS) else [])
    + [os.environ.get("PATH", "")])

from chipchamp import ui                                   # noqa: E402
from chipchamp.config import Workspace                     # noqa: E402
from chipchamp.tools import all_tools                      # noqa: E402
from chipchamp.tools.context import ToolContext            # noqa: E402
from chipchamp.waves.render import ascii_timing            # noqa: E402
from chipchamp.waves.store import WaveStore                # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "soc")
PROJECT = os.path.join(ROOT, "efpga-fabulous")
WRAP = os.path.join(PROJECT, "user_design", "top_wrapper.v")
DESIGN_REL = "user_design/lfsr16.v"
T = all_tools()
C = ui.console()

LFSR_DESIGN = """\
`default_nettype none

// Written by the chipchamp eFPGA equivalence demo.
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
        if (rst)      r <= 16'hACE1;
        else if (en)  r <= {r[14:0], fb};

    assign io_out = {12'b0, r};
    assign io_oeb = 28'b0000000000000000000000000001;
endmodule
`default_nettype wire
"""


def banner(agent: bool, model: str = "") -> None:
    if agent:
        C.print("\n[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                "[bold]a model chooses every step[/]")
        C.print("[dim]  Shows AUTONOMY: the model authors a design, routes a "
                "bitstream, and must then PROVE the programmed fabric behaves "
                "like its RTL — a passing cosimulation, not a claim.[/]")
        C.print(f"[dim]  Driven by [/][bold]{model}[/][dim]. It may skip the "
                f"proof, wander, or fail — that is part of what is shown.[/]")
    else:
        C.print("\n[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[bold]every step below is hardcoded[/]")
        C.print("[dim]  Shows the PLATFORM: a real bitstream loaded into a "
                "simulated fabric, compared cycle-by-cycle against the raw "
                "RTL, and rendered so you can see them agree.[/]")
        C.print("[dim]  It does NOT show agent autonomy — run with --agent "
                "for that.[/]")
    C.print()


def footer(agent: bool, closed: bool) -> None:
    C.rule()
    if agent:
        C.print("[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                f"[dim]a model chose those steps — it "
                f"{'proved the bitstream' if closed else 'did not prove the bitstream'}.[/]")
        C.print("[dim]  Re-running may go differently: that variance IS the "
                "capability being demonstrated.[/]")
    else:
        C.print("[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[dim]those steps were hardcoded. The bitstream, the cosim "
                "and the waveforms were real; the decisions were not.[/]")
        C.print("[dim]  For the agent deciding itself: "
                "python demo_efpga_equiv.py --agent[/]")


def scene(n: str, title: str) -> None:
    C.print(f"\n[bold cyan]{n}[/] [bold]{title}[/]")
    C.print("[grey30]" + "─" * 74 + "[/]")


def regen_fabric_quietly() -> bool:
    r = subprocess.run(["FABulous", "-p", PROJECT, "run", "run_FABulous_fabric"],
                       capture_output=True, text=True, timeout=600, cwd=PROJECT)
    return "executed successfully" in (r.stdout + r.stderr)


def retarget(module: str) -> None:
    src = open(WRAP).read()
    cur = re.search(r"(\w+)\s+top_i\s*\(", src)
    open(WRAP, "w").write(src.replace(f"{cur.group(1)} top_i (",
                                      f"{module} top_i ("))


def chronogram(vcd: str, tb: str) -> str:
    """The last ~40 fabric-output edges, gold and DUT side by side. The sim
    spends most of its wall time shifting the bitstream into the config
    chain; the design only RUNS at the end, so the window anchors there."""
    ws = WaveStore.open(vcd)
    paths = [f"{tb}.clk", f"{tb}.I_top_gold", f"{tb}.I_top"]
    probe = ws.snapshot([f"{tb}.I_top"], 0, 10 ** 14, max_edges=100000)
    edges = probe["signals"].get(f"{tb}.I_top", {}).get("edges") or []
    t1 = edges[-1][0] if edges else 10 ** 9
    t0 = edges[-40][0] if len(edges) >= 40 else 0
    return ascii_timing(ws.snapshot(paths, t0, t1))


# ---- mode 1: scripted -------------------------------------------------------

def scripted(ctx) -> bool:
    scene("1 ·", "A fresh design, a routed bitstream (the demo-A arc, compressed)")
    T["fs.write"].handler(ctx, path=os.path.join(
        os.path.relpath(PROJECT, ROOT), DESIGN_REL), content=LFSR_DESIGN)
    retarget("lfsr16")
    f = T["efpga-fabulous.fabric"].handler(ctx)
    b = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN_REL)
    C.print(f"  [green]✓[/] [bold]{b.get('job')}[/] {b.get('summary')}")
    if b.get("status") != "passed":
        return False

    scene("2 ·", "Load that bitstream into a SIMULATED fabric, race it "
          "against the RTL")
    C.print("[dim]  The testbench instantiates both: the raw lfsr16 as the "
            "gold model, and the generated eFPGA netlist with the bitstream "
            "shifted into its config chain. Every cycle: !== compare.[/]")
    s = T["efpga-fabulous.simulate"].handler(ctx, design=DESIGN_REL)
    ok = s.get("status") == "passed" and s.get("sim_passed")
    C.print(f"  [{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] "
            f"[bold]{s.get('job')}[/] {s.get('summary')}")
    if not ok:
        return False

    scene("3 ·", "See them agree — gold above, programmed fabric below")
    vcd = os.path.join(ROOT, s["waveform"])
    C.print(chronogram(vcd, "lfsr16_tb"))
    C.print("  [dim]same PRBS, cycle for cycle — the bitstream IS the "
            "design. have_errors stayed 0 for the whole run.[/]")

    scene("4 ·", "Close it on evidence")
    rep = ctx.gate_status()
    if rep is not None:
        C.print("  " + ui.gate_ladder(rep))
    done = T["report.done"].handler(ctx, summary="authored lfsr16, routed its "
                                    "bitstream, and proved the programmed "
                                    "fabric matches the RTL cycle-for-cycle "
                                    "in cosimulation")
    C.print(f"  [bold]report.done[/] ({done['task_class']}): "
            f"[{'green' if done['accepted'] else 'red'}]"
            f"{'ACCEPTED' if done['accepted'] else 'REJECTED'}[/]")
    return bool(done["accepted"])


# ---- mode 2: agentic --------------------------------------------------------

PROMPT = (
    "This workspace's eFPGA project (`efpga-fabulous/`, a FABulous project) "
    "has a generated fabric.\n\n"
    "1. Write a NEW user design: a 16-bit maximal-length Fibonacci LFSR "
    "(taps 16,15,13,4), using exactly the port interface of "
    "`efpga-fabulous/user_design/sequential_16bit_en.v` (read it first). "
    "Save as `efpga-fabulous/user_design/<name>.v`, then retarget "
    "`top_wrapper.v` (it instantiates the current design as `<module> "
    "top_i (` — point it at yours).\n"
    "2. Produce a routed bitstream for it (design path relative to the "
    "project, e.g. `user_design/<name>.v`).\n"
    "3. PROVE the bitstream: `efpga-fabulous.simulate` loads it into a "
    "simulated fabric and races it against your RTL cycle-for-cycle. The "
    "deliverable is a PASSING cosimulation — a routed bitstream alone is "
    "not proof of behavior.\n"
    "4. Close the task citing the bitstream and cosim jobs."
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

    scene("0 ·", "Hand it over — author, route, PROVE")
    nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
    if os.path.exists(nb):
        os.rename(nb, nb + ".preblind")
    runs = os.path.join(ROOT, ".chipchamp", "runs")
    agentic.runs_snapshot = None
    if os.path.isdir(runs):
        agentic.runs_snapshot = runs + ".preblind"
        os.rename(runs, agentic.runs_snapshot)
        os.makedirs(runs)
        seq = os.path.join(agentic.runs_snapshot, "seq.txt")
        if os.path.exists(seq):
            shutil.copy2(seq, os.path.join(runs, "seq.txt"))
    C.print(f"\n  [cyan]»[/] {PROMPT.splitlines()[0]}")

    sess = Session.new(str(ctx.ws.dot / "sessions"))
    loop = AgentLoop(ctx, gw, session=sess, max_steps=max_steps,
                     on_event=_agent_events(ctx), disclose=local,
                     context_budget=budget_for(gw, ctx.ws.config))
    rec = ExperimentRecorder(ctx.ws.dot, label="demo-efpga-equiv-d",
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

    efpga = [j for j in ctx.task_jobs if j.kind == "efpga-fabulous"]

    def step_of(j):
        return (j.result.get("metrics") or {}).get("step")

    wrote = sorted(p for p in ctx.edits
                   if "user_design" in p and not p.endswith("top_wrapper.v"))
    routed = any(step_of(j) == "bitstream" and j.status == "passed"
                 and (j.result.get("metrics") or {}).get("routed")
                 for j in efpga)
    proved = any(step_of(j) == "simulate" and j.status == "passed"
                 and (j.result.get("metrics") or {}).get("sim_passed")
                 for j in efpga)
    closed = bool(m.get("reported_done"))

    def mark(ok):
        return "[green]✓[/]" if ok else "[red]✗[/]"

    C.print()
    C.print(f"  {mark(m['steps'] > 0)} took any action at all")
    C.print(f"  {mark(bool(wrote))} wrote a user design"
            + (f" [dim]({', '.join(wrote)})[/]" if wrote else ""))
    C.print(f"  {mark(routed)} produced a routed bitstream")
    C.print(f"  {mark(proved)} PROVED it: passing fabric-vs-RTL cosimulation")
    C.print(f"  {mark(closed)} closed it with an accepted report.done")
    C.print(f"\n  [dim]recorded as experiment[/] [bold]{exp.id}[/] "
            f"[dim](outcome: {exp.outcome})[/]")
    if out.get("text"):
        C.print()
        ui.markdown(out["text"][:700])
    return bool(wrote and routed and proved and closed)


# ---- main -------------------------------------------------------------------

def recover_stale_state() -> None:
    stale = False
    if os.path.exists(WRAP + ".presnap"):
        os.replace(WRAP + ".presnap", WRAP)
        stale = True
    nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
    if os.path.exists(nb + ".preblind"):
        if os.path.exists(nb):
            os.unlink(nb)
        os.rename(nb + ".preblind", nb)
        stale = True
    runs = os.path.join(ROOT, ".chipchamp", "runs")
    if os.path.isdir(runs + ".preblind"):
        merge_runs_back(runs, runs + ".preblind")
        stale = True
    if stale:
        C.print("[yellow]  recovered state left by a run that died "
                "mid-experiment[/]")


def merge_runs_back(runs: str, snap: str) -> None:
    if not os.path.isdir(runs):
        os.rename(snap, runs)
        return
    for d in os.listdir(runs):
        src, dst = os.path.join(runs, d), os.path.join(snap, d)
        if d == "seq.txt":
            try:
                hi = max(int(open(src).read() or 0),
                         int(open(dst).read() or 0)
                         if os.path.exists(dst) else 0)
                with open(dst, "w") as fh:
                    fh.write(str(hi))
            except ValueError:
                pass
        elif d.startswith("J-"):
            if os.path.exists(dst):
                shutil.rmtree(src, ignore_errors=True)
            else:
                os.replace(src, dst)
    shutil.rmtree(runs)
    os.rename(snap, runs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--model", default="ollama:qwen3.6:35b")
    ap.add_argument("--max-steps", type=int, default=60)
    a = ap.parse_args()

    from chipchamp.adapters.fabulous import FabulousAdapter
    if not FabulousAdapter().available() or not os.path.isfile(
            os.path.join(PROJECT, "fabric.csv")):
        C.print("[red]needs a FABulous project with a generated fabric[/] — "
                "run `python demo_efpga.py` once first.")
        return 2

    ctx = ToolContext(Workspace(ROOT))
    recover_stale_state()
    shutil.copy2(WRAP, WRAP + ".presnap")
    banner(a.agent, a.model)
    closed = False
    try:
        closed = agentic(ctx, a.model, a.max_steps) if a.agent else scripted(ctx)
    finally:
        nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
        if os.path.exists(nb + ".preblind"):
            if os.path.exists(nb):
                os.unlink(nb)
            os.rename(nb + ".preblind", nb)
        runs_snap = getattr(agentic, "runs_snapshot", None)
        if runs_snap and os.path.isdir(runs_snap):
            merge_runs_back(os.path.join(ROOT, ".chipchamp", "runs"),
                            runs_snap)
        if os.path.exists(WRAP + ".presnap"):
            os.replace(WRAP + ".presnap", WRAP)
        # sweep every design the run wrote (and its TB + build products)
        for v in set(os.listdir(os.path.join(PROJECT, "user_design"))) - \
                {"custom_prims.v", "sequential_16bit_en.v", "top_wrapper.v",
                 "sequential_16bit_en.csv", "sequential_16bit_en.vh",
                 "sequential_16bit_en.vhd", "pwm_breath.v"}:
            os.unlink(os.path.join(PROJECT, "user_design", v))
            tb = os.path.join(PROJECT, "Test",
                              os.path.splitext(v)[0] + "_tb.v")
            if os.path.exists(tb):
                os.unlink(tb)
        shutil.rmtree(os.path.join(PROJECT, "Test", "build"),
                      ignore_errors=True)
        footer(a.agent, closed)
    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(main())
