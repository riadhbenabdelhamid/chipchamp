#!/usr/bin/env python
"""End-to-end demonstration of the Chipchamp platform loop (SPEC playbooks P1/P2/P7).

Deterministic, no model required — it drives the real tool catalog through a
ToolContext to show the whole loop on a genuine EDA toolchain:

    inject a bug  →  simulate (fail)  →  compare vs golden waves  →  fan-in cone
    →  root cause  →  fix  →  re-simulate (pass)  →  lint + coverage  →  gates
    →  signed evidence bundle

Run:  python demo.py   (from the repo root, with the venv active and OSS tools on PATH)
"""
from __future__ import annotations

import os
import shutil
import sys

from chipchamp.config import Workspace
from chipchamp.tools import all_tools
from chipchamp.tools.context import ToolContext

ROOT = os.path.join(os.path.dirname(__file__), "examples", "soc")
FIFO = os.path.join(ROOT, "rtl", "sync_fifo.sv")
T = all_tools()


def rule(title):
    print("\n" + "═" * 78 + f"\n  {title}\n" + "═" * 78)


def call(ctx, name, **kw):
    print(f"  → {name}({', '.join(f'{k}={v}' for k, v in kw.items())})")
    return T[name].handler(ctx, **kw)


def main():
    ws = Workspace(ROOT)
    ws.db(rebuild=True)
    ctx = ToolContext(ws)
    clean_src = open(FIFO).read()
    try:
        return _run(ws, ctx, clean_src)
    finally:
        # ALWAYS put the source back. This used to live at the end of the happy
        # path, so a crash mid-demo left the injected bug in the tree — and the
        # next run captured "golden" waves *of the bug*, compared them against
        # the bug, found no divergence and crashed too. Three runs looked like
        # three different faults; it was one un-restored file.
        with open(FIFO, "w") as fh:
            fh.write(clean_src)
        print("\n(demo restored the pristine sync_fifo.sv)")


def _run(ws, ctx, clean_src):

    rule("0. Baseline: index the design")
    db = ctx.db
    print(f"  indexed {len(db.modules)} modules, tops={db.tops()}")
    print("  " + call(ctx, "design.module", module="sync_fifo")["card"]["summary"])

    rule("1. Capture a golden waveform from the known-good design")
    g = call(ctx, "sim.run", test="fifo_smoke", seed=1)
    print(f"    {g['job']}: {g['sim_status']}")
    golden = os.path.join(ROOT, ".chipchamp", "golden.vcd")
    os.makedirs(os.path.dirname(golden), exist_ok=True)
    shutil.copy(ctx.runner.get(g["job"]).artifacts["waves"], golden)
    print(f"    golden waves saved: {os.path.relpath(golden, ROOT)}")

    rule("2. A bad commit lands: read data taken from the WRITE pointer")
    # This is the injected defect (as if a teammate committed it).
    buggy = clean_src.replace("assign rd_data = mem[rptr];",
                              "assign rd_data = mem[wptr];  // BUG: wrong pointer")
    with open(FIFO, "w") as fh:
        fh.write(buggy)
    ws._db_cache.clear()
    print("    sync_fifo.sv: `mem[rptr]` → `mem[wptr]`")

    rule("3. Triage: the smoke test now fails")
    s = call(ctx, "sim.run", test="fifo_smoke", seed=1)
    print(f"    {s['job']}: {s['sim_status']} — {s['summary']}")
    for h in ctx.runner.grep_log(s["job"], "CHIPCHAMP_FAIL", window=0)[:1]:
        print("    log:", h.strip().splitlines()[-1])

    rule("4. Query, don't dump: compare failing run vs golden; walk the cone")
    from chipchamp.waves import WaveStore
    buggy_ws = ctx.resolve_wave(s["job"])
    golden_ws = WaveStore.open(golden, provenance="golden")
    div = buggy_ws.compare(golden_ws, scope="dut")["first_divergence"]
    print(f"    first divergence: {div['signal']} @ {div['time']} "
          f"(fail={div['value_a']} vs golden={div['value_b']})")
    cone = call(ctx, "design.cone", module="sync_fifo", signal="rd_data", depth=1)
    for st in cone["statements"][:2]:
        print("    cone:", st.splitlines()[-1])
    print("\n  ROOT CAUSE: rd_data is driven from mem[wptr] instead of mem[rptr];")
    print("  the read port returns the most-recently-written entry, not the head.")

    rule("5. Fix at the source (tracked for the gates)")
    r = call(ctx, "fs.edit", path="rtl/sync_fifo.sv",
             old="assign rd_data = mem[wptr];  // BUG: wrong pointer",
             new="assign rd_data = mem[rptr];")
    print(f"    {r}")

    rule("6. Re-verify from the bottom of the ladder")
    print("    " + str(call(ctx, "lint.run", top="soc_top")["errors"]) + " lint error(s)")
    s2 = call(ctx, "sim.run", test="fifo_smoke", seed=1)
    print(f"    re-sim {s2['job']}: {s2['sim_status']}")
    cv = call(ctx, "cov.run", test="fifo_smoke", set="fix")
    print(f"    coverage: {cv.get('summary', {}).get('total')}% total")

    rule("7. Policy + gates: can we claim done?")
    chk = call(ctx, "policy.check")
    print(f"    task class: {chk['task_class']}  required gates: {chk['required_gates']}")
    done = call(ctx, "report.done", summary="Fix sync_fifo read pointer")
    print(f"    report.done accepted: {done['accepted']}")
    for g in done["gate_table"]:
        mark = {"pass": "✅", "fail": "❌", "missing": "⚠️"}.get(g["status"], "?")
        print(f"      {mark} {g['name']:18} {g['status']:8} {g['detail'][:48]}")

    rule("8. Signed evidence bundle")
    b = call(ctx, "evidence.bundle", narrative=(
        "sync_fifo returned mem[wptr] on the read port, exposing the newest "
        "entry instead of the FIFO head. Restored mem[rptr]; smoke test passes, "
        "lint clean, coverage non-regressing."))
    print(f"    bundle: {b['path']}")
    print(f"    signature: {b['signature'][:40]}…  verify: {b['verify']}  "
          f"gates: {b['all_gates_passed']}")

    return 0 if done["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
