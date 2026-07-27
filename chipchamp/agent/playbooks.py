"""Scripted playbooks (SPEC §10) — the shipped skills, runnable without a model.

These orchestrate the tool catalog deterministically, so the platform loop is
demonstrable air-gapped (D4) and testable. The model-driven loop uses the same
tools for open-ended work; these encode the fixed procedures (P1 triage, P6 lint
burn-down) as evidence-gathering pipelines.
"""
from __future__ import annotations

import os

from ..brand import markers
from ..jobs import RegressionManager
from ..jobs.regression import normalize_signature
from ..tools.context import ToolContext
from ..tools.verif_tools import _resolve_sim


def run_optimize(ctx: ToolContext, top: str = "", cov_set: str = "default") -> dict:
    """P11: PPA optimization brief — deterministic evidence gathering for an
    area/power pass. Collects the hotspots (area by RTL line), the free wins
    (dead-width from toggle coverage, strategy sweep Pareto), and the power
    picture (activity-driven when a routed design + workload exist), then
    hands the agent/human a ranked worklist. It deliberately does NOT edit
    RTL: the edit → LEC-proof → re-measure loop is L2 agent work, and every
    item here names the tools for that loop."""
    from ..tools import all_tools
    t = all_tools()
    top = top or (ctx.db.tops()[0] if ctx.db.tops() else "")
    brief: dict = {"top": top, "worklist": []}

    area = t["pd.area"].handler(ctx, top=top, by_line=True)
    if "error" not in area:
        brief["total_area_um2"] = area.get("total_area_um2")
        brief["area_hotspots"] = list(area.get("by_line", {}).items())[:5]
        brief["by_module"] = area.get("by_module", [])[:5]

    dw = t["pd.deadwidth"].handler(ctx, top=top, set=cov_set)
    if "error" in dw:
        brief["worklist"].append({
            "kind": "measurement", "target": "toggle coverage",
            "evidence": "no toggle coverage — dead-width analysis blind",
            "loop": f"cov.run (any smoke test, set={cov_set}) → pd.deadwidth"})
    elif dw.get("findings"):
        brief["deadwidth"] = dw["findings"][:5]
        for f in dw["findings"][:3]:
            brief["worklist"].append({
                "kind": "dead-width", "target": f"{f['signal']} ({f['source']})",
                "evidence": f["hint"],
                "est_saving_um2": f["wasted_area_um2"] or None,
                "loop": "fs.edit (narrow the width) → lec.run → pd.area"})

    sweep = t["pd.sweep"].handler(ctx, top=top)
    if "error" not in sweep:
        brief["sweep"] = {"table": sweep.get("table"),
                          "pareto": sweep.get("pareto")}
        best = min((r for r in sweep.get("table", []) if r.get("area_um2")),
                   key=lambda r: r["area_um2"], default=None)
        if best and best["config"] != "default":
            brief["worklist"].append({
                "kind": "synthesis-recipe",
                "target": f"strategy '{best['config']}'",
                "evidence": f"{best['area_um2']} µm² (sweep Pareto)",
                "loop": "pd.run with SYNTH_STRATEGY → pd.ppa"})

    pwr = t["pd.power"].handler(ctx, module=top)
    if "error" in pwr:
        brief["worklist"].append({
            "kind": "measurement", "target": "power",
            "evidence": "no routed design — power unmeasured",
            "loop": "pd.run → sim.run (workload) → pd.power activity_from=<job>"})
    if "error" not in pwr:
        brief["power"] = {"total_w": pwr.get("total_w"),
                          "clock_share_pct": pwr.get("clock_share_pct"),
                          "activity": pwr.get("activity"),
                          "warning": pwr.get("warning")}
        if (pwr.get("clock_share_pct") or 0) > 20:
            brief["worklist"].append({
                "kind": "clock-gating",
                "target": "clock tree",
                "evidence": f"clock = {pwr['clock_share_pct']}% of total power",
                "loop": "gate low-activity registers (design.domains shows "
                        "enables) → lec.run → pd.run → pd.power"})
        if pwr.get("warning"):
            brief["worklist"].append({
                "kind": "measurement",
                "target": "power activity",
                "evidence": "power is vectorless — untrusted for decisions",
                "loop": "sim.run (representative test) → pd.power "
                        "activity_from=<job>"})

    brief["note"] = ("brief only — apply an item, prove it with lec.run "
                     "(NFC), re-measure, and pd.ppa/pd.power deltas show the "
                     "win; report.done validates the gates")
    return brief


def run_triage(ctx: ToolContext, tag: str = "smoke", seeds=None,
               run_id: str = "triage") -> dict:
    """P1: run tests, cluster failures, and for each cluster gather root-cause
    evidence — representative log excerpt, run-vs-run first divergence, fan-in
    cone, and (if a git repo) suspect commits."""
    seeds = seeds or [1, 2, 3]
    tests = [t["name"] for t in (ctx.ws.tests_with_tag(tag) if tag else ctx.ws.tests)]
    if not tests:
        return {"error": f"no tests with tag '{tag}'"}

    def run_one(name, seed):
        from ..tools.verif_tools import _plan_for_test
        got = _plan_for_test(ctx, name, seed=seed, waves=True)
        if isinstance(got, dict):
            raise RuntimeError(f"{name}: {got['error']}")
        adapter, plan, files, timeout = got
        rec, _ = ctx.submit(plan, adapter, seed=seed, input_files=files,
                            timeout=timeout)
        return rec

    mgr = RegressionManager(run_one, max_parallel=4)
    res = mgr.run(tests, seeds, run_id=run_id)

    report = {"run_id": run_id, "summary": res.summary(),
              "passed": res.passed, "failed": res.failed, "flaky": res.flaky,
              "errored_tests": res.errored_tests, "clusters": []}
    # index passing runs (per test) to enable wave.compare
    passing = {}
    for rec in res.jobs:
        if rec.status == "passed" and "waves" in rec.artifacts:
            passing.setdefault(_test_of(rec), rec)

    for cl in res.clusters:
        rep = cl.representative
        rec = ctx.runner.get(rep["job"]) if rep else None
        cluster = {"id": cl.id, "count": cl.count, "signature": cl.signature,
                   "representative": rep, "evidence": {}}
        if rec:
            # 1. log excerpt
            hits = ctx.runner.grep_log(rec.id,
                                       "|".join(markers("FAIL")) + "|mismatch|Error|assert",
                                       window=1)
            cluster["evidence"]["log"] = hits[:2]
            # 2. wave compare vs a passing seed of the same test
            good = passing.get(rep["test"])
            if good and "waves" in rec.artifacts:
                a = ctx.resolve_wave(rec.id)
                b = ctx.resolve_wave(good.id)
                if a and b:
                    div = a.compare(b)
                    cluster["evidence"]["first_divergence"] = div.get("first_divergence")
                    # 3. cone of the diverging signal
                    d = div.get("first_divergence")
                    if d:
                        base = d["signal"].split(".")[-1]
                        for mod in ctx.db.modules:
                            c = ctx.db.cone(mod, base, "fanin", 2)
                            if c and c.statements:
                                cluster["evidence"]["cone"] = c.statements[:4]
                                cluster["evidence"]["cone_module"] = mod
                                break
            # 4. suspect commits (if a git repo)
            cluster["evidence"]["suspects"] = _blame(ctx)
        report["clusters"].append(cluster)
    return report


def run_lint_burndown(ctx: ToolContext, do_format: bool = True) -> dict:
    """P6 (evidence-gathering portion): snapshot lint, apply mechanical style
    formatting, re-lint, and report the delta. Substantive fixes are left to the
    model-driven loop; this establishes the before/after with real jobs."""
    from ..tools.build_tools import lint_run, style_format
    before = lint_run(ctx, top=ctx.target.top or None)
    result = {"before": {"errors": before.get("errors"),
                         "warnings": before.get("warnings"), "job": before.get("job")}}
    if do_format:
        fmt = style_format(ctx)
        result["format"] = {"status": fmt.get("status"), "job": fmt.get("job")}
    after = lint_run(ctx, top=ctx.target.top or None)
    result["after"] = {"errors": after.get("errors"),
                       "warnings": after.get("warnings"), "job": after.get("job")}
    result["remaining"] = after.get("diagnostics", [])[:20]
    return result


def run_triage_fanout(ctx: ToolContext, gateway_factory, tag: str = "smoke",
                      seeds=None, run_id: str = "triage",
                      max_concurrent: int = 3, on_event=None) -> dict:
    """P1 with §8.10 fan-out: run the regression, then spawn one read-only
    triage-analyst subagent per failure cluster; the orchestrator merges their
    root-cause analyses into the report. Falls back to the scripted evidence
    pipeline when no model gateway is available."""
    from .orchestrator import Orchestrator, SubagentTask

    report = run_triage(ctx, tag=tag, seeds=seeds, run_id=run_id)
    if "error" in report or not report.get("clusters"):
        return report
    orch = Orchestrator(ctx, gateway_factory, max_concurrent=max_concurrent,
                        on_event=on_event)
    tasks = []
    for cl in report["clusters"]:
        rep = cl.get("representative") or {}
        ev = cl.get("evidence", {})
        prompt = (
            f"Failure cluster {cl['id']} (count={cl['count']}).\n"
            f"Signature: {cl['signature']}\n"
            f"Representative: test={rep.get('test')} seed={rep.get('seed')} "
            f"job={rep.get('job')} waves_available={rep.get('waves')}\n"
            f"Scripted evidence so far: divergence={ev.get('first_divergence')} "
            f"log={ev.get('log', [])[:1]}\n"
            "Determine the root cause. Finish with: MECHANISM, LOCATION "
            "(file:line), CONFIDENCE (high/medium/low).")
        tasks.append(SubagentTask(role="triage-analyst", prompt=prompt,
                                  label=cl["id"]))
    for res in orch.run(tasks):
        for cl in report["clusters"]:
            if cl["id"] == res.label:
                cl["analysis"] = {"role": res.role, "text": res.text,
                                  "steps": res.steps, "jobs": res.jobs,
                                  "error": res.error}
    report["fanout"] = {"subagents": len(tasks), "max_concurrent": max_concurrent}
    return report


def _test_of(rec) -> str:
    for f in rec.input_files:
        if "/tb" in f or os.path.basename(f).startswith("tb_"):
            return os.path.basename(f)
    return rec.input_files[-1] if rec.input_files else ""


def _blame(ctx: ToolContext) -> list[dict]:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", ctx.ws.root, "log", "--oneline", "-5"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return [{"commit": ln.split()[0], "subject": " ".join(ln.split()[1:])}
                    for ln in out.stdout.strip().splitlines()]
    except (OSError, subprocess.SubprocessError):
        pass
    return []
