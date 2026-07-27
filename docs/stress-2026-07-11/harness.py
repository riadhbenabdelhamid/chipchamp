"""Stress-test orchestrator: cycle LM Studio models through chipchamp scenarios.

Per run: fresh workspace copy -> headless `chipchamp -p` under a hard wall-clock
cap -> objective validation -> one JSONL record. Resumable (done pairs are
skipped), tiered (core for everyone; extended for models that show competence;
heavy for the top performers), with a global deadline so the night ends.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scenarios import SCENARIOS, Scenario, ac, by_tier, session_stats  # noqa: E402

ROOT = Path(__file__).parent
TEMPLATE = ROOT / "ws_template"
RUNS = ROOT / "runs"
RESULTS = ROOT / "results.jsonl"
SOC = "/home/riadh/chipchamp/examples/soc"
CHIPCHAMP = "/home/riadh/chipchamp/.venv/bin/chipchamp"
LMS = "http://localhost:1234"

MODELS = [
    "qwen/qwen3.5-9b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-35b-a3b",
    "openai/gpt-oss-120b",
    "nvidia/nemotron-3-nano-omni",
    "liquid/lfm2-24b-a2b",
    "qwen3.5-9b-deepseek-v4-flash",
    "liquid/lfm2.5-1.2b",
]

EXCLUDES = [
    "--exclude", ".chipchamp/sessions", "--exclude", ".chipchamp/audit.jsonl",
    "--exclude", ".chipchamp/bundles", "--exclude", ".chipchamp/runs",
    "--exclude", ".chipchamp/pd", "--exclude", ".chipchamp/fpga",
    "--exclude", ".chipchamp/glsim", "--exclude", ".chipchamp/lec",
    "--exclude", ".chipchamp/power", "--exclude", ".chipchamp/ppa",
    "--exclude", ".chipchamp/sweep", "--exclude", ".chipchamp/bench",
    "--exclude", ".chipchamp/dashboard.html", "--exclude", ".chipchamp/ci-report.md",
    "--exclude", "obj_*", "--exclude", "cocotb_build", "--exclude", "__pycache__",
    "--exclude", "*.vvp", "--exclude", "*.vcd", "--exclude", "*.results.xml",
    "--exclude", "*.cov.xml", "--exclude", "coverage_*.dat",
]
KEEP_GOLDEN = ".chipchamp/golden.vcd"  # wave.compare baseline — must survive


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", model.lower())


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---- LM Studio ------------------------------------------------------------------


def lms_models() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{LMS}/api/v0/models", timeout=5) as r:
            return json.load(r).get("data", [])
    except Exception:
        return []


def warmup(model: str) -> dict:
    """JIT-load the model (auto-evicts the previous one); measure load time."""
    body = {"model": model, "messages": [{"role": "user", "content": "Reply: ok"}],
            "max_tokens": 8, "ttl": 7200}
    t0 = time.time()
    for attempt, payload in enumerate((body, {k: v for k, v in body.items()
                                              if k != "ttl"})):
        try:
            req = urllib.request.Request(
                f"{LMS}/v1/chat/completions", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=900) as r:
                json.load(r)
            break
        except Exception as e:
            if attempt == 1:
                return {"ok": False, "load_s": round(time.time() - t0, 1),
                        "error": str(e)[:200]}
    load_s = round(time.time() - t0, 1)
    loaded = [m["id"] for m in lms_models() if m.get("state") == "loaded"]
    info = next((m for m in lms_models() if m["id"] == model), {})
    return {"ok": True, "load_s": load_s, "loaded_now": loaded,
            "arch": info.get("arch", ""), "quant": info.get("quantization", ""),
            "params": info.get("params_string") or info.get("size_bytes", "")}


# ---- workspace ------------------------------------------------------------------


def build_template() -> None:
    if TEMPLATE.exists():
        shutil.rmtree(TEMPLATE)
    TEMPLATE.mkdir(parents=True)
    subprocess.run(["rsync", "-a", *EXCLUDES, f"{SOC}/", str(TEMPLATE) + "/"],
                   check=True)
    src_golden = Path(SOC) / KEEP_GOLDEN
    if src_golden.exists():
        dst = TEMPLATE / KEEP_GOLDEN
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_golden, dst)
    # deterministic pre-built design DB is fine to keep (rebuilds on staleness)
    log(f"template built at {TEMPLATE}")


def fresh_ws(model: str, scen: str) -> Path:
    d = RUNS / slug(model) / scen / "ws"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    subprocess.run(["rsync", "-a", f"{TEMPLATE}/", str(d) + "/"], check=True)
    return d


# ---- one run --------------------------------------------------------------------


def audit_stats(ws: Path) -> dict:
    p = ws / ".chipchamp" / "audit.jsonl"
    if not p.exists():
        return {}
    tin = tout = calls = 0
    first = last = None
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        calls += 1
        u = e.get("usage", {})
        tin += u.get("input_tokens", 0) or 0
        tout += u.get("output_tokens", 0) or 0
        first = first or e.get("ts")
        last = e.get("ts")
    return {"model_calls": calls, "tokens_in": tin, "tokens_out": tout,
            "audit_span_s": round((last - first), 1) if calls > 1 else 0}


def run_one(model: str, sc: Scenario, model_timeout: int) -> dict:
    ws = fresh_ws(model, sc.name)
    rundir = ws.parent
    try:
        sc.setup(str(ws))
    except Exception as e:
        return {"status": "setup-error", "error": str(e)}
    env = dict(os.environ,
               CHIPCHAMP_PROVIDER="lmstudio", CHIPCHAMP_MODEL=model,
               CHIPCHAMP_MODEL_TIMEOUT=str(model_timeout),
               CHIPCHAMP_MODEL_MAX_TOKENS="8192")
    cmd = ["timeout", "-k", "15", str(sc.timeout_s),
           CHIPCHAMP, "--root", str(ws), "--max-steps", str(sc.max_steps),
           "-p", sc.prompt]
    t0 = time.time()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    wall = round(time.time() - t0, 1)
    out = (p.stdout or "") + ("\n[stderr]\n" + p.stderr if p.stderr else "")
    (rundir / "stdout.log").write_text(out)
    timed_out = p.returncode == 124
    m = re.search(r"—\s*(\d+)\s*steps,\s*(\d+)\s*tokens", out)
    steps = int(m.group(1)) if m else None
    tokens = int(m.group(2)) if m else None
    extras = {k: int(mm.group(1)) for k, pat in
              (("recovered_calls", r"(\d+)\s*recovered-calls"),
               ("empty_turns", r"(\d+)\s*empty-turns"),
               ("length_stops", r"(\d+)\s*length-stops"))
              if (mm := re.search(pat, out))}
    st = session_stats(str(ws))
    aud = audit_stats(ws)
    try:
        v = sc.validate(str(ws), p.stdout or "")
    except Exception as e:
        v = {"checks": {}, "score": 0.0, "evidence": {"validator_error": str(e)[:300]}}
    model_err = bool(re.search(r"model (call failed|timed out)", out))
    rec = {
        "status": "done", "timed_out": timed_out, "rc": p.returncode,
        "wall_s": wall, "steps": steps, "tokens": tokens,
        "score": v.get("score", 0.0), "checks": v.get("checks", {}),
        "evidence": v.get("evidence", {}),
        "tool_calls": st.get("tool_calls", 0),
        "tool_errors": st.get("tool_errors", 0),
        "tool_names": st.get("tool_names", {}),
        "report_done_accepted": st.get("report_done_accepted"),
        "model_error": model_err,
        **extras,
        **{f"audit_{k}": v2 for k, v2 in aud.items()},
    }
    # keep failed workspaces for debugging; drop clean ones to save disk
    if rec["score"] >= 0.99:
        shutil.rmtree(ws, ignore_errors=True)
    return rec


# ---- orchestration --------------------------------------------------------------


def load_done() -> dict:
    done = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            try:
                r = json.loads(line)
                done[(r["model"], r["scenario"])] = r
            except Exception:
                pass
    return done


def append_result(rec: dict) -> None:
    with open(RESULTS, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--tiers", default="core,extended")
    ap.add_argument("--only-scenario", default="")
    ap.add_argument("--deadline", default="",
                    help="HH:MM local — stop launching new runs after this")
    ap.add_argument("--model-timeout", type=int, default=150)
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    models = [m for m in args.models.split(",") if m.strip()]
    tiers = [t for t in args.tiers.split(",") if t.strip()]
    deadline = None
    if args.deadline:
        h, mi = args.deadline.split(":")
        t = time.localtime()
        deadline = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(h), int(mi),
                                0, 0, 0, -1))
        if deadline < time.time():
            deadline += 86400

    if not TEMPLATE.exists():
        build_template()
    done = load_done()
    log(f"start: {len(models)} models × tiers {tiers}; "
        f"{len(done)} runs already recorded")

    for model in models:
        past_deadline = deadline and time.time() > deadline
        if past_deadline:
            log("deadline reached — stopping before next model")
            break
        # which scenarios does this model still owe?
        todo: list[Scenario] = []
        for tier in tiers:
            for sc in by_tier(tier):
                if args.only_scenario and sc.name != args.only_scenario:
                    continue
                prev = done.get((model, sc.name))
                if prev and not (args.retry_failed and prev.get("score", 0) < 0.5):
                    continue
                todo.append(sc)
        if not todo:
            log(f"{model}: nothing to do")
            continue

        w = warmup(model)
        log(f"{model}: warmup load_s={w.get('load_s')} ok={w.get('ok')} "
            f"quant={w.get('quant', '?')}")
        if not w.get("ok"):
            for sc in todo:
                append_result({"model": model, "scenario": sc.name,
                               "status": "model-load-failed", "score": 0.0,
                               "warmup": w, "ts": time.time()})
            continue

        core_scores: list[float] = [
            r.get("score", 0) for (m2, s2), r in done.items()
            if m2 == model and any(sc.name == s2 for sc in by_tier("core"))]
        zero_tools = 0
        for sc in todo:
            if deadline and time.time() > deadline:
                log("deadline reached — stopping")
                return
            # competence gate for the extended tier
            if sc.tier == "extended":
                cs = [r.get("score", 0) for (m2, s2), r in load_done().items()
                      if m2 == model and s2 in
                      [c.name for c in by_tier("core")]]
                if cs and sum(cs) < 1.2:
                    log(f"{model}/{sc.name}: SKIP extended (core sum "
                        f"{sum(cs):.2f} < 1.2)")
                    append_result({"model": model, "scenario": sc.name,
                                   "status": "skipped-competence-gate",
                                   "score": 0.0, "ts": time.time()})
                    continue
            log(f"{model} ▶ {sc.name} (cap {sc.timeout_s}s)")
            rec = run_one(model, sc, args.model_timeout)
            rec.update({"model": model, "scenario": sc.name, "tier": sc.tier,
                        "ts": time.time(), "warmup_load_s": w.get("load_s")})
            append_result(rec)
            done[(model, sc.name)] = rec
            log(f"{model} ◀ {sc.name}: score={rec.get('score')} "
                f"wall={rec.get('wall_s')}s steps={rec.get('steps')} "
                f"tool_calls={rec.get('tool_calls')} "
                f"errors={rec.get('tool_errors')} "
                f"timeout={rec.get('timed_out')}")
            if rec.get("tool_calls", 0) == 0:
                zero_tools += 1
                if zero_tools >= 2:
                    log(f"{model}: DNF — two scenarios with zero tool calls; "
                        f"skipping the rest")
                    for sc2 in todo[todo.index(sc) + 1:]:
                        append_result({"model": model, "scenario": sc2.name,
                                       "status": "dnf-no-tool-calling",
                                       "score": 0.0, "ts": time.time()})
                    break
    log("matrix pass complete")


if __name__ == "__main__":
    main()
