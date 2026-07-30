#!/usr/bin/env python
"""Waveform-debugging demo — the same bug, driven two different ways.

    python demo_waveform.py            # 1. SCRIPTED  — the platform works
    python demo_waveform.py --agent    # 2. AGENTIC   — the model steers

Both run identical machinery: a real Icarus simulation, a real job record, the
real gate evaluation, a real ``report.done``. Nothing is stubbed in either
mode. The ONLY difference is who chooses the next step — this file, or a model.

That distinction is stated on screen in both modes, at the top and again at the
end, because it is the one thing a viewer cannot infer from the output. A
scripted run and an autonomous run look almost identical while proving very
different things, and letting someone mistake the first for the second would be
the most misleading thing this demo could do.

The bug is a one-character off-by-one in sync_fifo's `full` flag — the kind
people genuinely ship — chosen because it is visible in the waveform as a
*relationship*: a flag flat at zero while a counter climbs past its limit.
"""
from __future__ import annotations

import argparse
import os
import sys

from chipchamp import ui
from chipchamp.config import Workspace
from chipchamp.tools import all_tools
from chipchamp.tools.context import ToolContext
from chipchamp.waves.render import analyzer_view

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "soc")
FIFO = os.path.join(ROOT, "rtl", "sync_fifo.sv")
TEST = "fifo_smoke"
GOOD = "assign full  = (count == DEPTH[AW:0]);"
BUG = "assign full  = (count >  DEPTH[AW:0]);"
T = all_tools()
C = ui.console()

# the signals that make this bug legible, in the order a DV engineer reads
# them: what drives the write, the flag that should stop it, the level it
# guards, then the read side. clk must be present or the columns stop being
# clock cycles.
SIGNALS = ["tb_sync_fifo.clk", "tb_sync_fifo.wr_en", "tb_sync_fifo.full",
           "tb_sync_fifo.level", "tb_sync_fifo.rd_en", "tb_sync_fifo.empty"]


# ---- framing ----------------------------------------------------------------

def banner(agent: bool, model: str = "") -> None:
    if agent:
        C.print("\n[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                "[bold]a model chooses every step[/]")
        C.print("[dim]  Shows AUTONOMY: the model reads the failure, decides "
                "which tools to call, and fixes the RTL itself.[/]")
        C.print(f"[dim]  Driven by [/][bold]{model}[/][dim]. The outcome "
                f"depends on that model's ability to steer and call tools — it "
                f"may wander, skip the waveform, or fail outright. That is the "
                f"honest state of the art, and part of what is being shown.[/]")
    else:
        C.print("\n[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[bold]every step below is hardcoded[/]")
        C.print("[dim]  Shows the PLATFORM: real Icarus jobs, real waveforms, "
                "real gate validation, a real report.done.[/]")
        C.print("[dim]  It does NOT show agent autonomy — this file chose the "
                "steps, not a model. Run with --agent for that.[/]")
    C.print()


def footer(agent: bool, accepted: bool, note: str = "") -> None:
    C.rule()
    if agent:
        C.print("[bold white on dark_red]  DEMO 2 · AGENTIC  [/]  "
                f"[dim]a model chose those steps — "
                f"{'it closed the task' if accepted else 'it did not close the task'}"
                f".[/]")
        C.print("[dim]  Re-running may go differently: that variance IS the "
                "capability being demonstrated.[/]")
    else:
        C.print("[bold black on bright_yellow]  DEMO 1 · SCRIPTED  [/]  "
                "[dim]those steps were hardcoded. The jobs, waveform and gates "
                "were real; the decisions were not.[/]")
        C.print("[dim]  For the agent choosing them itself: "
                "python demo_waveform.py --agent[/]")
    if note:
        C.print(f"[dim]  {note}[/]")


def scene(n: str, title: str) -> None:
    C.print(f"\n[bold cyan]{n}[/] [bold]{title}[/]")
    C.print("[grey30]" + "─" * 74 + "[/]")


# ---- the scenario -----------------------------------------------------------

def inject(src: str) -> None:
    """A bad commit lands: `full` asserts one entry too late, so the FIFO
    accepts a write when it is already full and silently overruns."""
    with open(FIFO, "w") as fh:
        fh.write(src.replace(GOOD, BUG))


def show_waves(ctx, job: str, cycles: int = 24, around: int | None = None) -> bool:
    store = ctx.resolve_wave(job)
    if store is None:
        return False
    lines = analyzer_view(store, paths=SIGNALS, cycles=cycles, around=around,
                          width=min(100, C.width - 4))
    for ln in lines:
        C.print(ln)
    return bool(lines)


def first_overrun(ctx, job: str):
    """When did the design FIRST misbehave? Not when the testbench noticed.

    The testbench gives up some cycles later and keeps running after that, so
    the tail of the trace shows the aftermath. Asking the waveform for the
    first time the failing condition holds is what puts the picture on the
    moment the narration is about."""
    hit = T["wave.when"].handler(ctx, run=job, expr="level == 8 && full == 0")
    t = (hit or {}).get("time")
    return t if isinstance(t, int) else None


def warm_up(ref: str, budget: float = 900.0) -> bool:
    """Make the model resident BEFORE anything is timed.

    A cold load happens inside the first request, and on this class of machine
    that can be minutes — evicting another model first if one is resident. The
    request timeout then expires having produced nothing, which reads exactly
    like the model being incapable rather than merely absent. Three runs of
    this demo died that way (0 steps, ~590s, no output) with a 75 GB model
    holding the accelerator.
    """
    import json as _json
    import time
    import urllib.request
    if not ref.startswith("ollama:"):
        return True                     # only ollama is handled here
    model = ref.partition(":")[2]
    C.print(f"[dim]  warming [/][bold]{model}[/][dim] — a cold load inside the "
            f"first request would time out and look like failure…[/]")
    body = _json.dumps({"model": model, "prompt": "ok", "stream": False,
                        "keep_alive": "30m",
                        "options": {"num_ctx": 32768}}).encode()
    req = urllib.request.Request("http://localhost:11434/api/generate",
                                 data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=budget) as fh:
            fh.read()
    except Exception as e:
        C.print(f"  [red]could not warm {model}[/]: {type(e).__name__}. "
                f"[dim]Load it first with `ollama run {model}`.[/]")
        return False
    C.print(f"[dim]  resident after {time.time() - t0:.0f}s[/]\n")
    return True


def ladder(ctx) -> None:
    rep = ctx.gate_status()
    if rep is not None:
        C.print("  " + ui.gate_ladder(rep))


# ---- mode 1: scripted -------------------------------------------------------

def scripted(ctx, clean: str) -> bool:
    scene("1 ·", "The symptom — a smoke test that was passing now fails")
    inject(clean)
    ctx.ws.invalidate(ctx.target_name)
    r = T["sim.run"].handler(ctx, test=TEST, seed=1)
    C.print(f"  [red]✗[/] [bold]{r['job']}[/] {TEST} [red]{r['sim_status']}[/] "
            f"[dim]· {r.get('summary', '')}[/]")

    scene("2 ·", "The evidence — what the hardware actually did")
    t = first_overrun(ctx, r["job"])
    C.print(f"[dim]  wave.when(level == 8 && full == 0) → first true at "
            f"[/][bold]t={t}[/]" if t is not None else
            "[dim]  the testbench says the FIFO never reported full:[/]")
    C.print("[dim]  The window below is centred there — not on the end of the "
            "run, where only the aftermath is visible.[/]\n")
    show_waves(ctx, r["job"], around=t)
    C.print("\n  [yellow]level[/] reaches [bold]8[/] — the configured depth — "
            "while [yellow]full[/] is still low.")
    C.print("  [dim]The flag that should stop writes has not asserted, so the "
            "next write is accepted and the buffer overruns.[/]")

    scene("3 ·", "Localize it in the source, not the waveform")
    cone = T["design.cone"].handler(ctx, module="sync_fifo", signal="full",
                                    depth=1)
    for st in (cone.get("statements") or [])[:1]:
        C.print(f"  [dim]drives[/] [bold]full[/]: {st.splitlines()[-1].strip()}")
    C.print("  [bold]root cause[/]: `count > DEPTH` is true only PAST the depth; "
            "at exactly DEPTH the FIFO still looks not-full.")

    scene("4 ·", "Fix at the source")
    old = open(FIFO).read()
    T["fs.edit"].handler(ctx, path="rtl/sync_fifo.sv", old=BUG, new=GOOD)
    ui.diff(old, open(FIFO).read(), "rtl/sync_fifo.sv")

    scene("5 ·", "Prove it — same test, same seed")
    lint = T["lint.run"].handler(ctx, top="soc_top")
    C.print(f"  [green]✓[/] lint [bold]{lint.get('errors', '?')}[/] error(s)")
    r2 = T["sim.run"].handler(ctx, test=TEST, seed=1)
    ok = r2.get("sim_status") == "pass"
    C.print(f"  [{'green' if ok else 'red'}]{'✓' if ok else '✗'}[/] "
            f"[bold]{r2['job']}[/] {TEST} [{'green' if ok else 'red'}]"
            f"{r2['sim_status']}[/]")
    cov = T["cov.run"].handler(ctx, test=TEST, set="demo")
    C.print(f"  [green]✓[/] coverage "
            f"[bold]{(cov.get('summary') or {}).get('total', '?')}%[/] "
            f"[dim](the coverage gate compares against the baseline)[/]")

    scene("6 ·", "Close it — gates decide, not the narrator")
    ladder(ctx)
    done = T["report.done"].handler(ctx, summary="Fix sync_fifo full flag")
    for g in done["gate_table"]:
        mark = {"pass": "[green]✓[/]", "fail": "[red]✗[/]",
                "missing": "[yellow]·[/]"}.get(g["status"], "?")
        C.print(f"    {mark} {g['name']:20} [dim]{g['detail'][:46]}[/]")
    verdict = "ACCEPTED" if done["accepted"] else "REJECTED"
    C.print(f"\n  [bold]report.done[/]: "
            f"[{'green' if done['accepted'] else 'red'}]{verdict}[/]")
    return bool(done["accepted"])


# ---- mode 2: agentic --------------------------------------------------------

PROMPT = (
    f"The test `{TEST}` is failing. Find out why by looking at the waveform, "
    f"fix the RTL, re-run the test, and close the task properly.\n\n"
    f"Work from evidence: read the failing job's log for the testbench's own "
    f"message, query the waveform to see what the signals actually did, and "
    f"cite the file:line you changed."
)


def agentic(ctx, clean: str, model: str, max_steps: int) -> bool:
    from chipchamp.agent import AgentLoop, Session
    from chipchamp.agent.working_set import budget_for
    from chipchamp.cli import _agent_events, _build_gateway

    # "ollama:devstral:24b" is provider:model — _build_gateway takes them
    # separately, and passing the whole ref as the model silently produces an
    # unavailable gateway on the DEFAULT provider (which is how the first run
    # of this demo did nothing at all and still called itself a success).
    prov, _, mdl = (model or "").partition(":")
    gw, reason, _audit = _build_gateway(ctx, mdl or None, prov or None)
    if gw is None:
        C.print(f"[red]no model available[/] — {reason}")
        C.print("[dim]pick one with `chipchamp model`, or pass "
                "--model ollama:devstral:24b[/]")
        return False

    scene("0 ·", "Inject the same bug, then hand the problem to the model")
    inject(clean)
    ctx.ws.invalidate(ctx.target_name)
    C.print(f"  [dim]sync_fifo.sv: `count == DEPTH` → `count > DEPTH`[/]")
    C.print(f"\n  [cyan]»[/] {PROMPT.splitlines()[0]}")

    from chipchamp.bench.experiment import ExperimentRecorder

    # Mirror what the REPL does for a local server, which building the loop
    # directly bypasses: defer the tool schemas (91 tools is ~8.4k tokens of
    # JSON before the task is even stated) and give the model a window big
    # enough to hold the prompt. ollama defaults num_ctx to 4096; chipchamp's
    # system prompt does not fit in that even with disclosure on, and the
    # symptom is a silent multi-minute stall rather than an error.
    local = gw.ref.startswith(("ollama:", "lmstudio:", "vllm:", "llamacpp:"))
    if local:
        gw.options = {**(gw.options or {}), "num_ctx": 32768}
        if not warm_up(gw.ref):
            return False
    sess = Session.new(str(ctx.ws.dot / "sessions"))
    loop = AgentLoop(ctx, gw, session=sess, max_steps=max_steps,
                     on_event=_agent_events(ctx), disclose=local,
                     context_budget=budget_for(gw, ctx.ws.config))
    rec = ExperimentRecorder(ctx.ws.dot, label="demo-agentic", task=PROMPT,
                             model=gw.ref)
    rec.attach(loop)
    out = loop.run(PROMPT)
    exp = rec.finish(out, max_steps=max_steps)

    scene("·", "What the model actually did")
    m = exp.metrics
    C.print(f"  model [bold]{exp.model}[/] · steps [bold]{m['steps']}[/] · "
            f"tool calls [bold]{m['tool_calls']}[/] "
            f"([red]{m['tool_failures']} failed[/]) · jobs "
            f"[bold]{m['jobs']}[/] · {m['wall_s']:.0f}s")
    for name, st in sorted((m.get("tools") or {}).items()):
        C.print(f"    [dim]·[/] {name} ×{st['calls']}"
                + (f" [red]({st['failed']} failed)[/]" if st["failed"] else ""))
    ladder(ctx)

    # An EMPTY change set passes the gates vacuously, so "gates green" is not
    # evidence of anything on its own — the first run of this demo announced
    # success for a model that never executed a single step. Judge on what the
    # model actually DID, and reserve "closed it" for an accepted report.done,
    # the platform's own definition of done.
    edited = sorted(ctx.edits)
    looked = any(t.startswith(("wave.", "mcp.wavelets.", "job.log"))
                 for t in (m.get("tools") or {}))
    passing = any(j.kind == "sim" and j.status == "passed" for j in ctx.task_jobs)
    closed = bool(m.get("reported_done"))

    def mark(ok):
        return "[green]✓[/]" if ok else "[red]✗[/]"

    C.print()
    C.print(f"  {mark(m['steps'] > 0)} took any action at all")
    C.print(f"  {mark(looked)} consulted the waveform / job log")
    C.print(f"  {mark(bool(edited))} edited the RTL"
            + (f" [dim]({', '.join(edited)})[/]" if edited else ""))
    C.print(f"  {mark(passing)} got {TEST} passing afterwards")
    C.print(f"  {mark(closed)} closed it with an accepted report.done")
    C.print(f"\n  [dim]recorded as experiment[/] [bold]{exp.id}[/] "
            f"[dim](outcome: {exp.outcome}) — chipchamp experiment show {exp.id}[/]")
    if out.get("text"):
        C.print()
        ui.markdown(out["text"][:700])
    return closed


# ---- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", action="store_true",
                    help="let a model drive instead of the script")
    ap.add_argument("--model", default="", help="e.g. ollama:devstral:24b")
    ap.add_argument("--max-steps", type=int, default=25)
    a = ap.parse_args()

    ws = Workspace(ROOT)
    ws.db(rebuild=True)
    ctx = ToolContext(ws)
    clean = open(FIFO).read()
    if BUG in clean:
        C.print("[yellow]sync_fifo.sv already carries the injected bug[/] — "
                "restore it with `git checkout` before running.")
        return 2

    banner(a.agent, a.model or "the configured model")
    accepted = False
    try:
        accepted = (agentic(ctx, clean, a.model, a.max_steps) if a.agent
                    else scripted(ctx, clean))
    finally:
        # ALWAYS restore. demo.py once crashed mid-run, left the injected bug
        # in the tree, and the next run captured "golden" waves OF the bug and
        # compared it against itself — three runs looked like three faults.
        with open(FIFO, "w") as fh:
            fh.write(clean)
        footer(a.agent, accepted, "sync_fifo.sv restored.")
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main())
