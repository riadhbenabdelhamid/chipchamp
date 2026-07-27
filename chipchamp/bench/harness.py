"""ChipchampBench (SPEC §17.1): the loop is the product, so benchmark the loop.

B1 — mutation detection/repair: inject realistic mutants into a copy of the
example workspace, verify the test suite CATCHES each one (detection rate = the
platform's smoke set is doing its job), and optionally hand the failing
workspace to the model-driven agent to measure autonomous fix rate.

B6 — triage fidelity: for detected mutants, run the scripted triage playbook and
score whether its evidence points at the mutated file (localization accuracy).

Everything runs on real tools in an isolated copy; results are machine-readable
JSON for trend tracking (§17.2).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from ..config import Workspace
from ..tools.context import ToolContext
from ..tools.verif_tools import _resolve_sim
from .mutators import Mutant, generate_mutants


def _fresh_workspace(src_root: str, dest: Path) -> Workspace:
    ignore = shutil.ignore_patterns(".chipchamp", "obj_cov", "*.vcd", "*.vvp",
                                    "coverage.dat", "work")
    shutil.copytree(src_root, dest, ignore=ignore)
    (dest / ".chipchamp").mkdir()
    # copy config bits the workspace needs (tests.yaml, config.toml, policy)
    for f in ("config.toml", "tests.yaml", "policy.yaml"):
        s = Path(src_root) / ".chipchamp" / f
        if s.exists():
            shutil.copy(s, dest / ".chipchamp" / f)
    return Workspace(str(dest))


def _run_test(ctx: ToolContext, test: str, seed: int = 1) -> str:
    files, tb_top = _resolve_sim(ctx, test)
    adapter = ctx.registry.for_role("sim")
    plan = adapter.sim(files, tb_top=tb_top, workdir=ctx.ws.root, seed=seed,
                       waves=False)
    rec, _ = ctx.submit(plan, adapter, input_files=files, seed=seed, timeout=120)
    return rec.status


def run_b1(example_root: str, work_dir: str, *, target_file: str = "rtl/sync_fifo.sv",
           test: str = "fifo_smoke", max_mutants: int = 8,
           on_event=None) -> dict:
    """B1 mutation-detection: mutants the smoke test kills / survives."""
    on_event = on_event or (lambda m: None)
    src = open(os.path.join(example_root, target_file)).read()
    mutants = generate_mutants(src, target_file)[:max_mutants]
    results = []
    t0 = time.time()
    for i, m in enumerate(mutants):
        dest = Path(work_dir) / f"mutant-{i}"
        ws = _fresh_workspace(example_root, dest)
        with open(dest / target_file, "w") as fh:
            fh.write(m.mutated)
        ctx = ToolContext(ws)
        status = _run_test(ctx, test)
        detected = status != "passed"
        on_event({"mutant": m.id, "line": m.line, "detected": detected})
        results.append({"mutant": m.id, "file": m.file, "line": m.line,
                        "description": m.description, "sim_status": status,
                        "detected": detected})
        shutil.rmtree(dest, ignore_errors=True)
    detected = sum(1 for r in results if r["detected"])
    return {"suite": "B1-mutation-detection", "target": target_file, "test": test,
            "mutants": results, "total": len(results), "detected": detected,
            "detection_rate": round(detected / len(results), 3) if results else None,
            "survivors": [r["mutant"] for r in results if not r["detected"]],
            "wall_s": round(time.time() - t0, 1)}


def run_b6(example_root: str, work_dir: str, *, target_file: str = "rtl/sync_fifo.sv",
           tag: str = "fifo", max_mutants: int = 3, on_event=None) -> dict:
    """B6 triage fidelity: does scripted triage localize the mutated file?"""
    from ..agent.playbooks import run_triage
    on_event = on_event or (lambda m: None)
    src = open(os.path.join(example_root, target_file)).read()
    mutants = [m for m in generate_mutants(src, target_file)][:max_mutants]
    scored = []
    for i, m in enumerate(mutants):
        dest = Path(work_dir) / f"triage-{i}"
        ws = _fresh_workspace(example_root, dest)
        with open(dest / target_file, "w") as fh:
            fh.write(m.mutated)
        ctx = ToolContext(ws)
        report = run_triage(ctx, tag=tag, seeds=[1], run_id=f"b6-{i}")
        localized = False
        n_clusters = len(report.get("clusters", []))
        base = os.path.basename(target_file).replace(".sv", "")
        for cl in report.get("clusters", []):
            ev = cl.get("evidence", {})
            blob = " ".join(str(x) for x in (ev.get("cone", []),
                                             ev.get("first_divergence"),
                                             ev.get("log", [])))
            if base in blob or target_file in blob:
                localized = True
        detected = report.get("failed", 0) > 0
        on_event({"mutant": m.id, "detected": detected, "localized": localized})
        scored.append({"mutant": m.id, "detected": detected,
                       "clusters": n_clusters, "localized": localized})
        shutil.rmtree(dest, ignore_errors=True)
    det = [s for s in scored if s["detected"]]
    loc = [s for s in det if s["localized"]]
    return {"suite": "B6-triage-fidelity", "target": target_file,
            "mutants": scored,
            "localization_rate": round(len(loc) / len(det), 3) if det else None}
