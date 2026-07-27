#!/usr/bin/env python
"""Phase 3 — drive every wave-0..4 feature end to end against local ollama
models, recording each run as an Experiment for later autopsy.

Deliberately NOT a unit test. The unit suite proves each mechanism in isolation
with a fake gateway; this proves the mechanisms survive contact with a real
model that was not written to cooperate — which is the only way to learn that,
say, a 4B model never calls tools.load, or that the disclosure menu changes
which tools get used at all.

One workspace copy per (model, scenario) so a run cannot inherit another's job
cache, history or notebook. laguna-s-2.1 is excluded on purpose: 75 GB is too
slow to run a matrix against.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/home/riadh/chipchamp")

from chipchamp.agent import AgentLoop, Session               # noqa: E402
from chipchamp.agent.working_set import budget_for           # noqa: E402
from chipchamp.bench.experiment import ExperimentRecorder    # noqa: E402
from chipchamp.config import Workspace                       # noqa: E402
from chipchamp.tools.context import ToolContext              # noqa: E402

SRC = Path("/home/riadh/chipchamp/examples/soc")
LAB = Path("/tmp/claude-1000/-home-riadh-chipchamp/"
           "c9b08b22-9d5d-4475-84f2-e44d61dd6535/scratchpad/lab")

# laguna-s-2.1 (75 GB) deliberately excluded — too slow for a matrix
MODELS = ["ollama:qwen3-coder:30b", "ollama:devstral:24b",
          "ollama:gpt-oss:20b", "ollama:qwen3.6:35b",
          "ollama:nemotron-3-nano:4b"]

# Each scenario names the wave features it is meant to exercise, and `check`
# reads the finished ToolContext/experiment to say whether they actually fired.
SCENARIOS = {
    "cache": {
        "wave": "1 (job cache) + 2 (ladder, history)",
        "prompt": ("Run lint on this workspace. Then run the test `fifo_smoke` "
                   "at seed 1. Then run `fifo_smoke` at seed 1 a second time. "
                   "Report the three results."),
        "steps": 12,
    },
    "disclosure": {
        "wave": "1b (progressive tool disclosure)",
        "prompt": ("How large is the counter module after synthesis, in um2? "
                   "The tools you need are not loaded yet — load the group you "
                   "need with tools.load first, then answer."),
        "steps": 10,
    },
    "detach": {
        "wave": "3 (detachable jobs)",
        "prompt": ("Start the test `fifo_smoke` in the BACKGROUND using "
                   "sim.run with detach=true. While it runs, call "
                   "design.hierarchy. Then collect the result with job.wait "
                   "and report the verdict."),
        "steps": 12,
    },
    "notebook": {
        "wave": "4 (project notebook)",
        "prompt": ("Record one durable finding about this project with "
                   "note.add: kind=constraint, about the sync_fifo module's "
                   "interface. Then list the notebook with note.list."),
        "steps": 8,
    },
    "spawn": {
        "wave": "4 (agent.spawn fan-out)",
        "prompt": ("Use agent.spawn to run TWO reviewers in parallel: one "
                   "labelled 'design' and one labelled 'verification', each "
                   "reviewing the sync_fifo module through its lens. "
                   "Summarize what they found."),
        "steps": 8,
    },
    "budget": {
        "wave": "1c (budget enforcement)",
        "prompt": ("Run the test `fifo_smoke` at seeds 1, 2, 3, 4 and 5 and "
                   "report which passed."),
        "steps": 14,
        "tiny_token_budget": True,
    },
}


def fresh_workspace(tag: str) -> Workspace:
    dest = LAB / tag
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns(
        ".chipchamp", "obj_cov*", "obj_tb*", "*.vcd", "*.vvp", "work",
        "coverage*.dat", "cocotb_build"))
    (dest / ".chipchamp").mkdir(exist_ok=True)
    for f in ("config.toml", "tests.yaml", "policy.yaml"):
        s = SRC / ".chipchamp" / f
        if s.exists():
            shutil.copy(s, dest / ".chipchamp" / f)
    return Workspace(str(dest))


def warm(ref: str, timeout: float = 420.0) -> float:
    """Load the model BEFORE the clock starts.

    First attempt recorded qwen3-coder:30b as `timeout, steps=0, 0 tools` —
    18 GB had simply not finished loading inside the 300s per-call budget. That
    is an infrastructure artifact wearing the costume of a model result, and
    recording it would poison the autopsy it exists to inform. `keep_alive`
    holds the model resident across the scenarios that follow, so the load is
    paid once per model rather than once per call.
    """
    import json as _json
    import urllib.request
    model = ref.partition(":")[2]
    # keep_alive is short on purpose. A long one wedged the lab: ollama held a
    # 21 GB model resident and would not evict it to make room for the next,
    # so the following model's warm blocked until IT timed out. Each model
    # needs the GPU only for its own scenarios.
    body = _json.dumps({"model": model, "prompt": "ok", "stream": False,
                        "keep_alive": "20m",
                        "options": {"num_ctx": 32768}}).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        fh.read()
    return time.time() - t0


def build_gateway(ws: Workspace, ref: str, timeout: float):
    from chipchamp.agent import ModelGateway, ModelRegistry
    provider_name, _, model = ref.partition(":")
    reg = ModelRegistry(ws.config, str(ws.dot))
    prov = reg.provider(provider_name)
    if prov is None:
        raise SystemExit(f"no provider '{provider_name}'")
    return ModelGateway(prov, model, audit_log=str(ws.dot / "audit.jsonl"),
                        max_tokens=4096, timeout=timeout,
                        options={"num_ctx": 32768})


def check(name: str, ctx: ToolContext, exp, out: dict) -> dict:
    """Did the feature actually fire? Reads state, never the model's prose."""
    tools = (exp.metrics.get("tools") or {})
    jobs = exp.metrics.get("job_detail") or []
    got = {}
    if name == "cache":
        got["job_cache_hit"] = any(j.get("cached") for j in jobs)
        got["ladder_evaluable"] = ctx.gate_status() is not None
        got["history_recorded"] = bool(ctx.history._load())
    elif name == "disclosure":
        got["tools_load_called"] = "tools.load" in tools
        got["deferred_tool_used"] = any(
            t.startswith(("synth.", "pd.")) for t in tools)
    elif name == "detach":
        got["detached_job"] = any(
            getattr(r, "detached", False) for r in ctx.task_jobs)
        got["job_wait_called"] = "job.wait" in tools
    elif name == "notebook":
        got["note_recorded"] = bool(ctx.notebook.entries())
        got["digest_nonempty"] = bool(ctx.notebook.digest())
    elif name == "spawn":
        got["spawn_called"] = "agent.spawn" in tools
        got["spawn_ok"] = tools.get("agent.spawn", {}).get("failed", 1) == 0
    elif name == "budget":
        got["budget_blocked"] = any(
            e["kind"] == "budget_block" for e in exp.timeline)
    got["compacted"] = exp.metrics.get("compactions", 0) > 0
    return got


def run_one(ref: str, name: str, spec: dict, timeout: float) -> dict:
    tag = f"{ref.replace(':', '_').replace('/', '_')}__{name}"
    print(f"\n=== {ref}  ·  {name} ({spec['wave']})", flush=True)
    started = time.time()
    try:
        ws = fresh_workspace(tag)
        ctx = ToolContext(ws)
        gw = build_gateway(ws, ref, timeout)
        if spec.get("tiny_token_budget"):
            # a ceiling low enough that a real run trips it — this is the only
            # way to see the pause happen rather than trust the unit test
            ctx.ledger.budget.model_tokens = 4000
        sess = Session.new(str(ws.dot / "sessions"))
        loop = AgentLoop(ctx, gw, session=sess, max_steps=spec["steps"],
                         disclose=True, context_budget=budget_for(gw, ws.config))
        rec = ExperimentRecorder(ws.dot, label=name, task=spec["prompt"],
                                 model=ref)
        rec.attach(loop)
        out = loop.run(spec["prompt"])
        exp = rec.finish(out, max_steps=spec["steps"], save=False)
        checks = check(name, ctx, exp, out)
        rec.note(f"feature checks: {json.dumps(checks)}")
        exp.metrics["feature_checks"] = checks
        rec.save()
        print(f"    outcome={exp.outcome} steps={exp.metrics['steps']} "
              f"wall={exp.metrics['wall_s']:.0f}s "
              f"ctx_peak={exp.metrics['context_chars_max']}", flush=True)
        print(f"    checks: {checks}", flush=True)
        return {"model": ref, "scenario": name, "outcome": exp.outcome,
                "metrics": exp.metrics, "checks": checks,
                "experiment": exp.id, "workspace": str(ws.root)}
    except Exception as e:
        print(f"    HARNESS ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return {"model": ref, "scenario": name, "outcome": "harness_error",
                "error": f"{type(e).__name__}: {e}",
                "wall_s": round(time.time() - started, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--timeout", type=float, default=420.0)
    ap.add_argument("--out", default=str(LAB / "results.json"))
    a = ap.parse_args()
    LAB.mkdir(parents=True, exist_ok=True)
    results = []
    for ref in [m for m in a.models.split(",") if m]:
        try:
            print(f"\n### warming {ref} …", flush=True)
            print(f"### resident after {warm(ref):.0f}s", flush=True)
        except Exception as e:
            # a model that cannot even load is a fact about the lab, recorded
            # as such rather than as six scenario failures
            print(f"### {ref} FAILED TO LOAD: {type(e).__name__}: {e}", flush=True)
            results.append({"model": ref, "scenario": "-",
                            "outcome": "unavailable", "error": str(e)[:200]})
            Path(a.out).write_text(json.dumps(results, indent=2, default=str))
            continue
        dead = 0
        for name in [s for s in a.scenarios.split(",") if s]:
            r = run_one(ref, name, SCENARIOS[name], a.timeout)
            results.append(r)
            Path(a.out).write_text(json.dumps(results, indent=2, default=str))
            # Three timeouts with zero steps is not six data points, it is one
            # fact stated three times — qwen3-coder burned 21 minutes proving
            # it could not answer at all. Move on and say so.
            dead = dead + 1 if (r["outcome"] == "timeout"
                                and not r.get("metrics", {}).get("steps")) else 0
            if dead >= 2:
                print(f"### {ref}: {dead} zero-step timeouts — skipping the rest",
                      flush=True)
                results.append({"model": ref, "scenario": "(remaining)",
                                "outcome": "skipped",
                                "error": "model produced no step in any scenario"})
                break
        # release the GPU for the next model rather than waiting for keep_alive
        os.system(f"ollama stop {ref.partition(':')[2]} >/dev/null 2>&1")
    print(f"\nwrote {a.out} ({len(results)} runs)")


if __name__ == "__main__":
    main()
