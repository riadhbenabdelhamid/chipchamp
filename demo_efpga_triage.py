#!/usr/bin/env python
"""eFPGA triage demo v2 — is my program broken, or my chip?

    python demo_efpga_triage.py            # 1. SCRIPTED  — the platform works
    python demo_efpga_triage.py --agent    # 2. AGENTIC   — the model steers

The uniquely-eFPGA debugging question. A user design that built yesterday
fails to route today — and the fault is in the *fabric's source*: a bad
"routing optimization" commit collapsed the LUT4AB switch-matrix muxes to a
single input each. Every destination is still driven, so fabric generation
passes its own validation; the routing graph just quietly lost ~30% of its
edges, and nextpnr blames the innocent design ("Routing design failed").

v1 of this scenario corrupted a *generated* artifact — and the model healed it
by accident with a clean-rebuild-first `with_fabric=true`, never observing the
failure. v2 closes that hatch: the corruption lives in source, so regeneration
faithfully reproduces it. That also changes the deliverable. Deleted routing
knowledge cannot be conjured back by inspection, so the agent's job is the
DIAGNOSIS — filed as a durable `note.add(root_cause)`, which is checkable
state, not prose — with repair-by-revert reserved for whoever holds the
history (the scripted mode plays that role).

Both modes say on screen which they are, twice, because a viewer cannot infer
it from the output.
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

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "soc")
PROJECT = os.path.join(ROOT, "efpga-fabulous")
MATRIX = os.path.join(PROJECT, "Tile", "LUT4AB", "LUT4AB_switch_matrix.list")
CONFIGMEM = os.path.join(PROJECT, "Tile", "LUT4AB", "LUT4AB_ConfigMem.csv")
PIPS = os.path.join(PROJECT, ".FABulous", "pips.txt")
DESIGN = "user_design/sequential_16bit_en.v"
T = all_tools()
C = ui.console()


def banner(agent: bool, model: str = "") -> None:
    if agent:
        C.print("\n[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                "[bold]a model chooses every step[/]")
        C.print("[dim]  Shows AUTONOMY: the model must decide whether the "
                "fault is in the user design or in the fabric, prove it, and "
                "file the diagnosis. A clean rebuild will NOT make this one "
                "go away.[/]")
        C.print(f"[dim]  Driven by [/][bold]{model}[/][dim]. It may blame the "
                f"innocent design, wander, or fail — that is part of what is "
                f"being shown.[/]")
    else:
        C.print("\n[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[bold]every step below is hardcoded[/]")
        C.print("[dim]  Shows the PLATFORM: a real routing failure whose root "
                "cause is in the fabric's source, attributed by elimination "
                "and repaired by revert.[/]")
        C.print("[dim]  It does NOT show agent autonomy — run with --agent "
                "for that.[/]")
    C.print()


def footer(agent: bool, closed: bool) -> None:
    C.rule()
    if agent:
        C.print("[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                f"[dim]a model chose those steps — it "
                f"{'delivered the diagnosis' if closed else 'did not deliver the diagnosis'}.[/]")
        C.print("[dim]  Re-running may go differently: that variance IS the "
                "capability being demonstrated.[/]")
    else:
        C.print("[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[dim]those steps were hardcoded. The failure, the evidence "
                "and the repair were real; the decisions were not.[/]")
        C.print("[dim]  For the agent deciding itself: "
                "python demo_efpga_triage.py --agent[/]")


def scene(n: str, title: str) -> None:
    C.print(f"\n[bold cyan]{n}[/] [bold]{title}[/]")
    C.print("[grey30]" + "─" * 74 + "[/]")


def pips_lines() -> int:
    try:
        with open(PIPS) as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def regen_fabric_quietly() -> bool:
    """Regenerate outside the tool catalog, so demo setup does not put jobs
    into the evidence pool the agent (or the gates) will be judged on."""
    r = subprocess.run(["FABulous", "-p", PROJECT, "run", "run_FABulous_fabric"],
                       capture_output=True, text=True, timeout=300, cwd=PROJECT)
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
    starting (the grow demo lost a notebook to in-memory-only snapshots)."""
    stale = False
    for pth in (MATRIX, CONFIGMEM):
        if os.path.exists(pth + ".presnap"):
            os.replace(pth + ".presnap", pth)
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
                "mid-experiment; regenerating the healthy fabric[/]")
        regen_fabric_quietly()


def _collapse_muxes(text: str) -> tuple[str, int]:
    """`N1BEG[0|0|0|0],[a|b|c|d]` → `N1BEG0,a`: every destination keeps ONE
    driver (generation still validates) while the routing fabric loses its
    choices — the plausible shape of a bad 'mux pruning' optimization."""
    def expand(tok):
        m = re.search(r"\[([^\]]+)\]", tok)
        if not m:
            return [tok]
        return [tok[:m.start()] + a + tok[m.end():]
                for a in m.group(1).split("|")]

    out, n = [], 0
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and not s.upper().startswith("INCLUDE") \
                and "," in s:
            l, r = [p.strip() for p in s.split(",", 1)]
            L, R = expand(l), expand(r)
            if len(L) > 1 and len(set(L)) == 1 and len(R) == len(L):
                out.append(f"{L[0]},{R[0]}")
                n += 1
                continue
        out.append(ln)
    return "\n".join(out) + "\n", n


def inject() -> int:
    """The bad commit lands and its author 'helpfully' rebuilds the fabric.

    ConfigMem.csv is derived from the matrix and must be regenerated with it —
    exactly what a fabric author's flow does — and the design's stale build
    products are swept so the evidence does not argue with itself."""
    starved, n = _collapse_muxes(open(MATRIX).read())
    with open(MATRIX, "w") as fh:
        fh.write(starved)
    if os.path.exists(CONFIGMEM):
        os.unlink(CONFIGMEM)
    if not regen_fabric_quietly():
        raise RuntimeError("injection failed: starved fabric did not generate")
    base = os.path.join(PROJECT, os.path.splitext(DESIGN)[0])
    for ext in (".bin", ".fasm", ".json", "_npnr_log.txt"):
        if os.path.exists(base + ext):
            os.unlink(base + ext)
    return n


# ---- mode 1: scripted -------------------------------------------------------

def scripted(ctx) -> bool:
    scene("1 ·", "Yesterday: the design builds")
    T["efpga-fabulous.fabric"].handler(ctx)
    b = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN)
    healthy = pips_lines()
    C.print(f"  [green]✓[/] [bold]{b.get('job')}[/] {b.get('summary')}")
    C.print(f"  [dim]routing graph: {healthy:,} edges[/]")
    if b.get("status") != "passed":
        return False

    scene("2 ·", "A fabric-source commit lands: 'switch-matrix mux pruning'")
    n = inject()
    C.print(f"[dim]  {n} LUT4AB muxes collapsed to a single input in "
            f"Tile/LUT4AB/LUT4AB_switch_matrix.list. Generation still "
            f"validates — every destination has a driver — but the routing "
            f"graph is now [/][bold]{pips_lines():,}[/][dim] edges of "
            f"{healthy:,}. Nobody touched the user design.[/]")

    scene("3 ·", "Today: the same design fails — and blames itself")
    b2 = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN)
    C.print(f"  [red]✗[/] [bold]{b2.get('job')}[/] {b2.get('summary')}")
    for h in ctx.runner.grep_log(b2.get("job", ""),
                                 "Failed to find a route|Routing design failed",
                                 window=0)[:2]:
        C.print(f"  [dim]log:[/] [red]{h.strip().splitlines()[-1][:76]}[/]")

    scene("4 ·", "Eliminate: is it stale build state? Rebuild the fabric")
    T["efpga-fabulous.fabric"].handler(ctx)
    b3 = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN)
    C.print(f"  [red]✗[/] [bold]{b3.get('job')}[/] still fails after a clean "
            f"fabric rebuild — [bold]this is not v1's stale-artifact story[/]. "
            f"The fault reproduces from source.")

    scene("5 ·", "Attribute into the source, and file the diagnosis")
    C.print(f"  [dim]design diff: empty · routing graph {pips_lines():,} vs "
            f"{healthy:,} yesterday · the delta traces to the switch-matrix "
            f"muxes[/]")
    note = T["note.add"].handler(
        ctx, kind="root_cause",
        subject="fabric routing starvation in LUT4AB switch matrix",
        detail="LUT4AB_switch_matrix.list muxes were collapsed to single "
               "inputs by a 'pruning' commit; generation validates (all "
               "destinations driven) but the routing graph lost ~30% of its "
               "edges and known-good designs no longer route. Design innocent.",
        evidence=f"{b2.get('job')}, {b3.get('job')}")
    C.print(f"  [green]✓[/] root-cause filed to the notebook "
            f"[dim]({note.get('id')}) — future sessions inherit it[/]")

    scene("6 ·", "The fix belongs to whoever holds the history: revert")
    # the bad commit touched TWO files — the matrix and its derived config
    # memory. Reverting only the matrix leaves a 616-bit fabric against a
    # 382-bit ConfigMem and generation fails: a revert is the whole commit
    # or it is not a revert. (The first run of this scene proved it.)
    rel = os.path.relpath(MATRIX, ROOT)
    relc = os.path.relpath(CONFIGMEM, ROOT)
    T["fs.write"].handler(ctx, path=rel, content=scripted.healthy_matrix)
    T["fs.write"].handler(ctx, path=relc, content=scripted.healthy_configmem)
    C.print(f"  [green]✓[/] reverted {rel} [dim]+ its derived ConfigMem[/]")
    T["efpga-fabulous.fabric"].handler(ctx)
    b4 = T["efpga-fabulous.bitstream"].handler(ctx, design=DESIGN)
    ok = b4.get("status") == "passed"
    C.print(f"  [{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] "
            f"[bold]{b4.get('job')}[/] {b4.get('summary')} · graph back to "
            f"[bold]{pips_lines():,}[/] edges")

    scene("7 ·", "Close it — the revert is an edit, so the E gates are owed")
    rep = ctx.gate_status()
    if rep is not None:
        C.print("  " + ui.gate_ladder(rep))
    done = T["report.done"].handler(ctx, summary="reverted the switch-matrix "
                                    "pruning commit; root cause filed; design "
                                    "was never at fault")
    C.print(f"  [bold]report.done[/] ({done['task_class']}): "
            f"[{'green' if done['accepted'] else 'red'}]"
            f"{'ACCEPTED' if done['accepted'] else 'REJECTED'}[/]")
    return ok and bool(done["accepted"])


# ---- mode 2: agentic --------------------------------------------------------

PROMPT = (
    f"Until recently, `{DESIGN}` produced a routed bitstream on this "
    f"workspace's eFPGA project (`efpga-fabulous/`, a FABulous project). Now "
    f"the build fails.\n\n"
    f"Find out what is actually at fault — the user design, or the fabric it "
    f"is being mapped onto — and prove it by elimination rather than "
    f"assumption.\n\n"
    f"You may not be able to repair what you find. The deliverable is the "
    f"DIAGNOSIS: record your conclusion with note.add (kind root_cause), "
    f"citing the job ids and files that support it, then close the task. If "
    f"the evidence shows a fix within your reach, make it — but a wrong fix "
    f"is worse than a right diagnosis."
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

    scene("0 ·", "Break the chip's SOURCE, then hand the mystery to the model")
    # BLIND THE TEST: the notebook digest rides into every system prompt, and
    # a prior run of this very demo filed the root cause there — the first
    # agentic run of v2 "diagnosed" the fault in zero jobs by paraphrasing its
    # own briefing. In production that is the notebook doing its job; in an
    # experiment it is the answer key on the subject's desk. Sequestered here,
    # restored in main()'s finally.
    nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
    if os.path.exists(nb):
        os.rename(nb, nb + ".preblind")   # on disk, so a crash can't lose it
    # Same treatment for the JOB STORE: it spans campaigns, so it holds
    # passing bitstreams from the healthy-fabric era next to failures from
    # the broken one — the third answer-shaped leak in three runs (a blinded
    # model closed on pure archaeology, zero jobs of its own). Moved aside,
    # merged back in main()'s finally; the runner's in-memory seq keeps new
    # job ids above the archive's high-water mark, so the merge cannot
    # collide.
    runs = os.path.join(ROOT, ".chipchamp", "runs")
    agentic.runs_snapshot = None
    if os.path.isdir(runs):
        agentic.runs_snapshot = runs + ".preblind"
        os.rename(runs, agentic.runs_snapshot)
        # an EMPTY store, not a missing one: the ctx's live runner keeps
        # writing here (run 3 of this campaign lost all four of the model's
        # build attempts to id allocation crashing on the vanished dir)
        os.makedirs(runs)
    healthy = pips_lines()
    n = inject()
    C.print(f"[dim]  {n} switch-matrix muxes collapsed at the source; fabric "
            f"regenerated from it ({pips_lines():,} of {healthy:,} edges). A "
            f"clean rebuild reproduces the fault. The model is told only that "
            f"the build used to work:[/]")
    C.print(f"\n  [cyan]»[/] {PROMPT.splitlines()[0]}")

    sess = Session.new(str(ctx.ws.dot / "sessions"))
    loop = AgentLoop(ctx, gw, session=sess, max_steps=max_steps,
                     on_event=_agent_events(ctx), disclose=local,
                     context_budget=budget_for(gw, ctx.ws.config))
    rec = ExperimentRecorder(ctx.ws.dot, label="demo-efpga-triage-v2",
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

    # Attribution is judged from state. The design's innocence makes the right
    # answer checkable without reading prose — except the diagnosis itself,
    # which the model files as a STRUCTURED claim (a root_cause note) that is
    # graded against ground truth like any answer would be.
    efpga = [j for j in ctx.task_jobs if j.kind == "efpga-fabulous"]

    def step_of(j):
        return (j.result.get("metrics") or {}).get("step")

    # A failing job only counts as reproducing THE failure if it is the same
    # FLOW that is broken. Run 5 closed 6/6 on the old grader by probing the
    # (independently broken) harden flow: its yosys `-y` crash counted as
    # "reproduced" and, coming after a fabric regen, as "ruled out" too —
    # shape-checks satisfied by the wrong failure entirely.
    reproduced = any(step_of(j) == "bitstream" and j.status != "passed"
                     for j in efpga)

    def regenerated_fabric(j):
        argv = " ".join(j.steps[0].argv) if j.steps else ""
        return "run_FABulous_fabric" in argv or step_of(j) == "fabric"

    # the elimination beat: a run that INCLUDED fabric regeneration still
    # failed to route the SAME flow (a fabric job followed by a failing
    # bitstream, or a with_fabric bitstream that failed)
    ruled_out = False
    fabric_seen = False
    for j in efpga:                       # submission order
        failing_bitstream = step_of(j) == "bitstream" and j.status != "passed"
        if regenerated_fabric(j):
            fabric_seen = True
            if failing_bitstream:
                ruled_out = True
        elif fabric_seen and failing_bitstream:
            ruled_out = True
    touched_design = sorted(p for p in ctx.edits if "user_design" in p)

    notes = ctx.notebook.entries("root_cause")
    # MECHANISM words only. "fabric" and "tile" are gone: run 5's note blamed
    # a Yosys version skew yet matched on "all 17 tiles". The injected fault
    # is routing starvation in the switch matrix — a right diagnosis cannot
    # avoid naming interconnect, however it phrases it.
    mech_words = ("routing", "route", "pip", "mux", "switch", "matrix",
                  "starv", "congest", "interconnect")
    diagnosis = next((e for e in notes
                      if any(w in (e["subject"] + " " + e["detail"]).lower()
                             for w in mech_words)), None)
    closed = bool(m.get("reported_done"))

    def mark(ok):
        return "[green]✓[/]" if ok else "[red]✗[/]"

    C.print()
    C.print(f"  {mark(m['steps'] > 0)} took any action at all")
    C.print(f"  {mark(reproduced)} reproduced the failure "
            f"(its own failing bitstream job)")
    C.print(f"  {mark(ruled_out)} ruled out stale build state "
            f"(rebuilt the fabric; the SAME failure persisted)")
    C.print(f"  {mark(not touched_design)} left the (innocent) user design "
            f"untouched"
            + (f" [red]({', '.join(touched_design)})[/]" if touched_design else ""))
    C.print(f"  {mark(bool(diagnosis))} filed a root-cause note naming the "
            f"interconnect mechanism"
            + (f" [dim]({diagnosis['id']}: {diagnosis['subject'][:44]})[/]"
               if diagnosis else ""))
    C.print(f"  {mark(closed)} closed it with an accepted report.done")
    C.print(f"\n  [dim]recorded as experiment[/] [bold]{exp.id}[/] "
            f"[dim](outcome: {exp.outcome})[/]")
    if out.get("text"):
        C.print()
        ui.markdown(out["text"][:700])
    return bool(reproduced and ruled_out and not touched_design
                and diagnosis and closed)


# ---- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--model", default="ollama:qwen3.6:35b")
    ap.add_argument("--max-steps", type=int, default=40)
    a = ap.parse_args()

    from chipchamp.adapters.fabulous import FabulousAdapter
    if not FabulousAdapter().available() or not os.path.isfile(MATRIX):
        C.print("[red]needs a FABulous project with a generated fabric[/] — "
                "run `python demo_efpga.py` once first.")
        return 2

    ctx = ToolContext(Workspace(ROOT))
    recover_stale_state()
    scripted.healthy_matrix = open(MATRIX).read()
    scripted.healthy_configmem = (open(CONFIGMEM).read()
                                  if os.path.exists(CONFIGMEM) else "")
    for pth in (MATRIX, CONFIGMEM):   # crash-safe restore points
        if os.path.exists(pth):
            shutil.copy2(pth, pth + ".presnap")
    banner(a.agent, a.model)
    closed = False
    try:
        closed = agentic(ctx, a.model, a.max_steps) if a.agent else scripted(ctx)
    finally:
        # ALWAYS put the fabric SOURCE back and regenerate, so every generated
        # artifact is coherent with the healthy source for whatever runs next
        nb = os.path.join(ROOT, ".chipchamp", "notebook.json")
        if os.path.exists(nb + ".preblind"):
            if os.path.exists(nb):
                os.unlink(nb)     # in-run notes are campaign ephemera
            os.rename(nb + ".preblind", nb)
        runs_snap = getattr(agentic, "runs_snapshot", None)
        if runs_snap and os.path.isdir(runs_snap):
            merge_runs_back(os.path.join(ROOT, ".chipchamp", "runs"),
                            runs_snap)
        for pth in (MATRIX, CONFIGMEM):
            if os.path.exists(pth + ".presnap"):
                os.replace(pth + ".presnap", pth)
        for d in __import__("glob").glob(os.path.join(PROJECT, "Tile", "*",
                                                      "macro")):
            shutil.rmtree(d, ignore_errors=True)
        regen_fabric_quietly()
        footer(a.agent, closed)
    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(main())
