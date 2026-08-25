#!/usr/bin/env python
"""eFPGA grow demo (E1) — the design doesn't fit, so grow the chip.

    python demo_efpga_grow.py            # 1. SCRIPTED  — the platform works
    python demo_efpga_grow.py --agent    # 2. AGENTIC   — the model steers

The inversion of the triage demo's question. There, the design was innocent
and the chip was broken; here the design is a hard REQUIREMENT — 32
independent LFSR channels, 512 flops — and the chip is simply too small: the
fabric holds 672 LUT+FF BELs and placement dies with "no BELs remaining".
The reflex every software-shaped agent has is to shrink the program. The
semiconductor-shaped move is to grow the silicon: the fabric is not fixed
hardware, it is GENERATED from source (`fabric.csv` is a floor plan you can
edit), so the right fix is more rows of logic tiles, a regenerated fabric,
and the same untouched design routing with room to spare.

Growth is a two-line discipline: rows are inserted as whole DSP_top/DSP_bot
pairs (the supertile must stay paired), and only above the south termination
so every IO coordinate the wrapper pins (X0Y1..X0Y14, counted from the north
edge) keeps its meaning.

Both modes say on screen which they are, twice, because a viewer cannot
infer it from the output.
"""
from __future__ import annotations

import argparse
import os
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

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "soc")
PROJECT = os.path.join(ROOT, "efpga-fabulous")
FABRIC = os.path.join(PROJECT, "fabric.csv")
WRAP = os.path.join(PROJECT, "user_design", "top_wrapper.v")
DESIGN = "user_design/lfsr_bank.v"
N_LFSR = 32          # 512 flops: decisively over the stock fabric's budget
GROW_PAIRS = 4       # +8 logic rows → 672 BELs become 1056
# Blinding/restore state lives OUTSIDE the agent's workspace root. In-tree
# ".presnap"/".preblind" names are archaeology bait: a triage model globbed
# the presnap next to the live file and unblinded itself.
SEQUESTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".demo_sequester")


def sequestered(pth: str) -> str:
    os.makedirs(SEQUESTER, exist_ok=True)
    return os.path.join(SEQUESTER, os.path.basename(pth))


# Agent-authored flow artifacts are an answer-shaped leak into the NEXT run
# (a blinded triage model read a prior run's probe/ logs as "pre-built
# tests"). Manifest the project at start, sweep anything new at the end.
def _manifest_path() -> str:
    return sequestered("project_manifest.json")


def snapshot_project_manifest() -> None:
    import json
    files = []
    for root, _dirs, names in os.walk(PROJECT):
        for n in names:
            files.append(os.path.relpath(os.path.join(root, n), PROJECT))
    with open(_manifest_path(), "w") as fh:
        json.dump(files, fh)


def sweep_new_project_files() -> bool:
    import json
    mp = _manifest_path()
    if not os.path.exists(mp):
        return False
    keep = set(json.load(open(mp)))
    for root, dirs, names in os.walk(PROJECT, topdown=False):
        for n in names:
            p = os.path.join(root, n)
            if os.path.relpath(p, PROJECT) not in keep:
                os.unlink(p)
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))   # only empties fall
            except OSError:
                pass
    os.unlink(mp)
    return True
T = all_tools()
C = ui.console()

BANK_DESIGN = f"""\
// The requirement: {N_LFSR} independent 16-bit LFSR channels, XOR-folded
// onto the pads. {N_LFSR * 16} flops — non-negotiable.
module lfsr_bank (
    input wire clk,
    input wire [27:0] io_in,
    output wire [27:0] io_out,
    output wire [27:0] io_oeb
);
    wire rst = io_in[0];
    wire en  = io_in[1];
    wire [15:0] acc [0:{N_LFSR}];
    assign acc[0] = 16'h0000;
    genvar i;
    generate
        for (i = 0; i < {N_LFSR}; i = i + 1) begin : bank
            reg [15:0] x;
            wire fb = ^(x & (16'hB400 ^ i[15:0]));
            always @(posedge clk)
                if (en)
                    if (rst) x <= 16'hACE1 ^ i[15:0];
                    else     x <= {{x[14:0], fb}};
            assign acc[i + 1] = acc[i] ^ x;
        end
    endgenerate
    assign io_out[15:0]  = acc[{N_LFSR}];
    assign io_out[27:16] = acc[{N_LFSR}][11:0] ^ acc[{N_LFSR // 2}][11:0];
    assign io_oeb = 28'd0;
endmodule
"""


def banner(agent: bool, model: str = "") -> None:
    if agent:
        C.print("\n[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                "[bold]a model chooses every step[/]")
        C.print("[dim]  Shows AUTONOMY: the design is a hard requirement that "
                "does not fit the chip. The model must realise the FABRIC is "
                "the thing to change — grow it, regenerate it, and deliver a "
                "routed bitstream without touching the design.[/]")
        C.print(f"[dim]  Driven by [/][bold]{model}[/][dim]. It may shrink the "
                f"design (forbidden), wander, or fail — that is part of what "
                f"is being shown.[/]")
    else:
        C.print("\n[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[bold]every step below is hardcoded[/]")
        C.print("[dim]  Shows the PLATFORM: a real capacity overflow, answered "
                "by editing the chip's floor plan and regenerating the "
                "silicon around the unchanged design.[/]")
        C.print("[dim]  It does NOT show agent autonomy — run with --agent "
                "for that.[/]")
    C.print()


def footer(agent: bool, closed: bool) -> None:
    C.rule()
    if agent:
        C.print("[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                f"[dim]a model chose those steps — it "
                f"{'grew the chip and delivered' if closed else 'did not deliver'}"
                f" the bitstream.[/]")
        C.print("[dim]  Re-running may go differently: that variance IS the "
                "capability being demonstrated.[/]")
    else:
        C.print("[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[dim]those steps were hardcoded. The overflow, the grown "
                "fabric and the routed bitstream were real; the decisions "
                "were not.[/]")
        C.print("[dim]  For the agent deciding itself: "
                "python demo_efpga_grow.py --agent[/]")


def scene(n: str, title: str) -> None:
    C.print(f"\n[bold cyan]{n}[/] [bold]{title}[/]")
    C.print("[grey30]" + "─" * 74 + "[/]")


def lut_bels() -> int:
    rows = [ln for ln in open(FABRIC) if "LUT4AB" in ln]
    return sum(ln.count("LUT4AB") for ln in rows) * 8


def grown(text: str, pairs: int) -> str:
    """Insert `pairs` copies of the last DSP_top/DSP_bot logic row-pair just
    above the south termination: supertile pairing intact, every existing
    tile keeps its X/Y name, the wrapper's pinned IO BELs stay valid."""
    lines = text.splitlines()
    s_term = next(i for i, ln in enumerate(lines) if "S_term" in ln)
    pair = lines[s_term - 2:s_term]
    assert "DSP_top" in pair[0] and "DSP_bot" in pair[1], pair
    return "\n".join(lines[:s_term] + pair * pairs + lines[s_term:]) + "\n"


def regen_fabric_quietly() -> bool:
    """Regenerate outside the tool catalog, so demo setup/teardown does not
    put jobs into the evidence pool the agent (or the gates) is judged on."""
    r = subprocess.run(["FABulous", "-p", PROJECT, "run", "run_FABulous_fabric"],
                       capture_output=True, text=True, timeout=600, cwd=PROJECT)
    return "executed successfully" in (r.stdout + r.stderr)


def merge_runs_back(runs: str, snap: str) -> None:
    """Fold the campaign store into the archive without ever crashing the
    restore: colliding J-dirs keep the ARCHIVE's copy (a collision means the
    campaign record's id was a lie), and seq.txt keeps the higher counter.
    A colliding os.replace once threw mid-finally and left the workspace
    stranded between stores."""
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


def recover_stale_state() -> None:
    """A killed run leaves its disk snapshots behind; put them back before
    starting. One process death mid-run proved the point: the in-memory
    snapshots died with it and the notebook lost its original entries."""
    stale = False
    for pth in (FABRIC, WRAP):
        # sequestered home first, legacy in-tree name for pre-fix corpses
        for snap in (sequestered(pth), pth + ".presnap"):
            if os.path.exists(snap):
                os.replace(snap, pth)
                stale = True
    nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
    for snap in (sequestered(nb), nb + ".preblind"):
        if os.path.exists(snap):
            if os.path.exists(nb):
                os.unlink(nb)
            os.rename(snap, nb)
            stale = True
    runs = os.path.join(ROOT, ".chipchamp", "runs")
    for snap in (sequestered(runs), runs + ".preblind"):
        if os.path.isdir(snap):
            merge_runs_back(runs, snap)
            stale = True
    if sweep_new_project_files():
        stale = True
    base = os.path.join(PROJECT, os.path.splitext(DESIGN)[0])
    for ext in (".v", ".vh", ".vhd", ".csv", ".bin", ".fasm", ".json",
                "_npnr_log.txt"):
        if os.path.exists(base + ext):
            os.unlink(base + ext)
            stale = True
    if stale:
        C.print("[yellow]  recovered state left by a run that died "
                "mid-experiment; regenerating the stock fabric[/]")
        regen_fabric_quietly()


def setup_requirement() -> None:
    """Place the oversized design and point the wrapper at it — direct file
    IO, not tools, so the edits ledger starts empty for whoever runs next."""
    with open(os.path.join(PROJECT, DESIGN), "w") as fh:
        fh.write(BANK_DESIGN)
    wrap = open(WRAP).read()
    with open(WRAP, "w") as fh:
        fh.write(wrap.replace("sequential_16bit_en top_i (",
                              "lfsr_bank top_i ("))
    base = os.path.join(PROJECT, os.path.splitext(DESIGN)[0])
    for ext in (".bin", ".fasm", ".json", "_npnr_log.txt"):
        if os.path.exists(base + ext):
            os.unlink(base + ext)


# ---- mode 1: scripted -------------------------------------------------------

def scripted(ctx) -> bool:
    scene("1 ·", f"The requirement: {N_LFSR} LFSR channels — "
          f"{N_LFSR * 16} flops, non-negotiable")
    rel = os.path.join(os.path.relpath(PROJECT, ROOT), DESIGN)
    T["fs.write"].handler(ctx, path=rel, content=BANK_DESIGN)
    T["fs.edit"].handler(ctx,
                         path=os.path.join(os.path.relpath(PROJECT, ROOT),
                                           "user_design/top_wrapper.v"),
                         old="sequential_16bit_en top_i (",
                         new="lfsr_bank top_i (")
    C.print(f"  [green]✓[/] wrote {rel} and retargeted the wrapper")

    scene("2 ·", "It does not fit the chip")
    C.print(f"  [dim]fabric budget: {lut_bels()} LUT+FF BELs[/]")
    b = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN)
    C.print(f"  [red]✗[/] [bold]{b.get('job')}[/] {b.get('summary')}")
    for h in ctx.runner.grep_log(b.get("job", ""), "no BELs remaining",
                                 window=0)[:1]:
        C.print(f"  [dim]log:[/] [red]{h.strip().splitlines()[-1][:76]}[/]")
    if b.get("status") == "passed":
        return False    # the overflow IS the premise

    scene("3 ·", "The software reflex is to shrink the program. "
          "The chip is generated — grow it instead")
    grown_src = grown(open(FABRIC).read(), GROW_PAIRS)
    T["fs.write"].handler(ctx, path=os.path.join(
        os.path.relpath(PROJECT, ROOT), "fabric.csv"), content=grown_src)
    C.print(f"  [green]✓[/] fabric.csv: +{GROW_PAIRS * 2} rows of logic tiles "
            f"inserted above the south termination "
            f"[dim](supertile pairs kept; IO coordinates preserved)[/]")

    scene("4 ·", "Regenerate the silicon")
    f = T["efpga-fabulous.fabric"].handler(ctx)
    C.print(f"  [green]✓[/] [bold]{f.get('job')}[/] {f.get('summary')} · "
            f"budget now [bold]{lut_bels()}[/] BELs")

    scene("5 ·", "The unchanged design routes on the grown chip")
    b2 = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN)
    ok = b2.get("status") == "passed"
    C.print(f"  [{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] "
            f"[bold]{b2.get('job')}[/] {b2.get('summary')}")

    scene("6 ·", "Close it — the fabric edit owes the E gates")
    rep = ctx.gate_status()
    if rep is not None:
        C.print("  " + ui.gate_ladder(rep))
    done = T["report.done"].handler(ctx, summary="design over fabric capacity; "
                                    "grew the fabric floor plan by "
                                    f"{GROW_PAIRS * 2} logic rows and "
                                    "regenerated; unchanged design routes")
    C.print(f"  [bold]report.done[/] ({done['task_class']}): "
            f"[{'green' if done['accepted'] else 'red'}]"
            f"{'ACCEPTED' if done['accepted'] else 'REJECTED'}[/]")
    return ok and bool(done["accepted"])


# ---- mode 2: agentic --------------------------------------------------------

PROMPT = (
    f"`{DESIGN}` is a hard requirement: {N_LFSR} independent LFSR channels "
    f"({N_LFSR * 16} flops). It must produce a routed bitstream on this "
    f"workspace's eFPGA project (`efpga-fabulous/`, a FABulous project). The "
    f"build currently fails.\n\n"
    f"You may NOT change the user design in any way — the design is the "
    f"requirement. The FABRIC is negotiable: it is generated from source "
    f"(`fabric.csv` is the floor plan; `Tile/` defines the tile types), and "
    f"regenerating it after a source edit is a normal, supported flow.\n\n"
    f"Deliver a routed bitstream for the unchanged design, citing the job "
    f"evidence, then close the task."
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

    scene("0 ·", "Plant the oversized requirement, then hand it over")
    # Blind the run the same way the triage campaign learned to: notebook out
    # (the answer key must not ride in on the system prompt), job archive out
    # (passing builds of OTHER designs invite archaeology instead of science).
    nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
    if os.path.exists(nb):
        os.rename(nb, sequestered(nb))   # on disk, outside the workspace
    runs = os.path.join(ROOT, ".chipchamp", "runs")
    agentic.runs_snapshot = None
    if os.path.isdir(runs):
        agentic.runs_snapshot = sequestered(runs)
        os.rename(runs, agentic.runs_snapshot)
        os.makedirs(runs)   # an EMPTY store, not a missing one
        seq = os.path.join(agentic.runs_snapshot, "seq.txt")
        if os.path.exists(seq):
            # seed the counter: ANY runner reading this store must number
            # above the archive's high-water mark, or its records collide
            # with history on merge
            shutil.copy2(seq, os.path.join(runs, "seq.txt"))
    setup_requirement()
    C.print(f"[dim]  {DESIGN} placed ({N_LFSR * 16} flops), wrapper "
            f"retargeted, fabric untouched at {lut_bels()} BELs. The model is "
            f"told the design is non-negotiable:[/]")
    C.print(f"\n  [cyan]»[/] {PROMPT.splitlines()[0]}")

    sess = Session.new(str(ctx.ws.dot / "sessions"))
    loop = AgentLoop(ctx, gw, session=sess, max_steps=max_steps,
                     on_event=_agent_events(ctx), disclose=local,
                     context_budget=budget_for(gw, ctx.ws.config))
    rec = ExperimentRecorder(ctx.ws.dot, label="demo-efpga-grow-e1",
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

    # Judged from state, with the triage campaign's grader lessons built in:
    # same-flow evidence only, and the discipline axis (design untouched) is
    # the one a software-shaped agent is most tempted to break.
    efpga = [j for j in ctx.task_jobs if j.kind == "efpga-fabulous"]

    def step_of(j):
        return (j.result.get("metrics") or {}).get("step")

    def overflowed(j):
        return bool(ctx.runner.grep_log(
            j.id, "Unable to place|no BELs remaining", window=0))

    # a failing bitstream only counts if it failed on CAPACITY — run 2 of
    # this demo "reproduced" with three wrapper-as-design synthesis crashes
    reproduced = any(step_of(j) == "bitstream" and j.status != "passed"
                     and overflowed(j) for j in efpga)
    delivered = any(step_of(j) == "bitstream" and j.status == "passed"
                    and (j.result.get("metrics") or {}).get("routed")
                    for j in efpga)
    touched_design = sorted(p for p in ctx.edits if "user_design" in p)
    grew_fabric = sorted(p for p in ctx.edits
                         if p.endswith("fabric.csv") or "/Tile/" in p
                         or p.startswith("Tile/"))
    regenerated = any(step_of(j) == "fabric" and j.status == "passed"
                      for j in efpga) or delivered
    closed = bool(m.get("reported_done"))

    def mark(ok):
        return "[green]✓[/]" if ok else "[red]✗[/]"

    C.print()
    C.print(f"  {mark(m['steps'] > 0)} took any action at all")
    C.print(f"  {mark(reproduced)} reproduced the overflow "
            f"(its own failing bitstream job)")
    C.print(f"  {mark(not touched_design)} left the (non-negotiable) design "
            f"untouched"
            + (f" [red]({', '.join(touched_design)})[/]" if touched_design else ""))
    C.print(f"  {mark(bool(grew_fabric))} edited the fabric source"
            + (f" [dim]({', '.join(grew_fabric[:2])})[/]" if grew_fabric else ""))
    C.print(f"  {mark(regenerated)} regenerated the fabric from it")
    C.print(f"  {mark(delivered)} delivered a routed bitstream for the "
            f"unchanged design")
    C.print(f"  {mark(closed)} closed it with an accepted report.done")
    C.print(f"\n  [dim]recorded as experiment[/] [bold]{exp.id}[/] "
            f"[dim](outcome: {exp.outcome})[/]")
    if out.get("text"):
        C.print()
        ui.markdown(out["text"][:700])
    return bool(reproduced and not touched_design and grew_fabric
                and regenerated and delivered and closed)


# ---- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--model", default="ollama:qwen3.6:35b")
    ap.add_argument("--max-steps", type=int, default=60)
    a = ap.parse_args()

    from chipchamp.adapters.fabulous import FabulousAdapter
    if not FabulousAdapter().available() or not os.path.isfile(FABRIC):
        C.print("[red]needs a FABulous project with a generated fabric[/] — "
                "run `python demo_efpga.py` once first.")
        return 2

    ctx = ToolContext(Workspace(ROOT))
    recover_stale_state()
    for pth in (FABRIC, WRAP):        # crash-safe restore points
        shutil.copy2(pth, sequestered(pth))
    snapshot_project_manifest()
    banner(a.agent, a.model)
    closed = False
    try:
        closed = agentic(ctx, a.model, a.max_steps) if a.agent else scripted(ctx)
    finally:
        nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
        if os.path.exists(sequestered(nb)):
            if os.path.exists(nb):
                os.unlink(nb)     # in-run notes are campaign ephemera
            os.rename(sequestered(nb), nb)
        runs_snap = getattr(agentic, "runs_snapshot", None)
        if runs_snap and os.path.isdir(runs_snap):
            merge_runs_back(os.path.join(ROOT, ".chipchamp", "runs"),
                            runs_snap)
        # the chip goes back to its stock floor plan, the wrapper to the
        # stock design, and every product of the oversized design is swept
        for pth in (FABRIC, WRAP):
            if os.path.exists(sequestered(pth)):
                os.replace(sequestered(pth), pth)
        sweep_new_project_files()
        base = os.path.join(PROJECT, os.path.splitext(DESIGN)[0])
        # includes the flow's sibling emissions (.vh/.vhd/.csv) — demo A's
        # cleanup lesson, relearned here on the first scripted run
        for ext in (".v", ".vh", ".vhd", ".csv", ".bin", ".fasm", ".json",
                    "_npnr_log.txt"):
            if os.path.exists(base + ext):
                os.unlink(base + ext)
        # harden attempts leave LibreLane run trees under Tile/*/macro —
        # ~20MB per tile of debris that later runs then excavate as "evidence"
        for d in __import__("glob").glob(os.path.join(PROJECT, "Tile", "*",
                                                      "macro")):
            shutil.rmtree(d, ignore_errors=True)
        regen_fabric_quietly()
        footer(a.agent, closed)
    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(main())
