"""Verification tools: sim, regression, waveforms, coverage, formal (SPEC §9 D–G)."""
from __future__ import annotations

import os

from ..coverage import CoverageService, ingest_verilator_dat
from ..jobs import RegressionManager
from ..waves import ascii_timing, wavejson
from .base import tool, truncate
from .context import ToolContext


def _runner_of(t) -> dict:
    """The runner config of a test, tolerant of both shapes: the canonical
    ``{name, runner: {kind, ...}}`` and a FLATTENED ``{name, kind, cpp, ...}``
    where the runner fields sit at the top level. Without this, a flattened
    ``kind: verilator_cpp`` test is silently mis-routed to the default iverilog
    sim (which then fails to compile the DUT) instead of the C++ harness."""
    if not t:
        return {}
    r = t.get("runner")
    return r if isinstance(r, dict) and r else t


def _resolve_sim(ctx: ToolContext, test: str):
    """Return (files, tb_top) for a test name or a bare testbench top."""
    t = ctx.find_test(test)
    tb_files = []
    if t:
        runner = _runner_of(t)
        tb_top = runner.get("tb_top") or f"tb_{test}"
        tb_files = [os.path.join(ctx.ws.root, f) for f in runner.get("files", [])]
    else:
        tb_top = test
    return ctx.target.sources + tb_files, tb_top


def _plan_for_test(ctx: ToolContext, test: str, seed: int = 1,
                   waves: bool = True, uvm_test: str = "", plusargs=None):
    """Full runner-kind dispatch for ONE named test — the single source of
    truth for how a test runs (plain SV / UVM / cocotb), shared by sim.run,
    regress.run, cov.run and the triage playbook. Returns
    ``(adapter, plan, files, timeout_s)`` or ``{"error": …}``.

    Regression/triage previously re-implemented this with the default sim
    adapter only, so UVM benches went to icarus (no uvm_macros.svh) and
    cocotb tests lost their Python runner — every such test 'errored' in a
    regression while passing standalone."""
    import inspect
    from ..config import rtl_only
    t = ctx.find_test(test)
    runner = _runner_of(t)
    if runner.get("kind") == "verilator_cpp":
        # system-level sim reusing components' C++ golden ref models
        # (SPEC §12 reuse): verilate the RTL `top` and link a C++ harness that
        # composes ref_<name>.hpp models + hole refs and checks the DUT.
        adapter = ctx.registry.get("verilator")
        if not adapter.available():
            return {"error": "verilator not available (needed for the C++ harness)"}
        rtl = rtl_only(ctx.target.sources)
        cpp = [os.path.join(ctx.ws.root, c) for c in runner.get("cpp", [])]
        if not cpp:
            return {"error": "verilator_cpp runner needs a 'cpp' harness file list"}
        cpp_incdirs = [os.path.join(ctx.ws.root, d)
                       for d in runner.get("cpp_incdirs", [])]
        top = runner.get("top") or runner.get("tb_top") or test
        plan = adapter.sim_cpp(rtl, cpp, top, ctx.ws.root,
                               cpp_incdirs=cpp_incdirs, incdirs=ctx.target.incdirs,
                               defines=ctx.target.defines, seed=seed)
        return adapter, plan, rtl, 300
    if runner.get("kind") == "cocotb":
        adapter = ctx.registry.get("cocotb")
        if not adapter.available():
            return {"error": "cocotb not available (need cocotb's python w/ VPI)"}
        # dedup by realpath: the `rtl:` list may re-name target sources
        seen: set[str] = set()
        rtl = []
        for f in ctx.target.sources + [os.path.join(ctx.ws.root, x)
                                       for x in runner.get("rtl", [])]:
            rp = os.path.realpath(f)
            if rp not in seen:
                seen.add(rp)
                rtl.append(f)
        plan = adapter.sim(rtl, runner.get("module"),
                           runner.get("toplevel") or runner.get("tb_top"),
                           ctx.ws.root, hdl_sim=runner.get("hdl_sim", "icarus"),
                           seed=seed, waves=waves,
                           test_dir=os.path.join(ctx.ws.root,
                                                 runner.get("test_dir", "tb")))
        return adapter, plan, rtl, 180
    files, tb_top = _resolve_sim(ctx, test)
    is_uvm = bool(uvm_test) or runner.get("kind") == "uvm" or bool(runner.get("uvm"))
    pargs = list(plusargs or [])
    utest = uvm_test or runner.get("uvm_test", "")
    if utest:
        pargs.append(f"UVM_TESTNAME={utest}")
    incdirs = [os.path.join(ctx.ws.root, d) for d in runner.get("incdirs", [])]
    adapter = ctx.registry.for_role("sim")
    if is_uvm:
        # UVM needs a class-based-SV simulator; icarus can't. Prefer one whose
        # sim() takes the uvm kwarg (verilator OSS path, questa/vcs licensed).
        for name in ("verilator", "questa", "vcs"):
            a = ctx.registry.get(name)
            if a.available() and "uvm" in inspect.signature(a.sim).parameters:
                adapter = a
                break
        else:
            return {"error": "no UVM-capable simulator available (need "
                             "verilator 5.x, questa or vcs)"}
    if adapter is None or not adapter.available():
        return {"error": "no simulator adapter available"}
    kw = {"uvm": True} if is_uvm else {}
    plan = adapter.sim(files, tb_top=tb_top, workdir=ctx.ws.root, seed=seed,
                       waves=waves, plusargs=pargs, incdirs=incdirs or None, **kw)
    return adapter, plan, files, (600 if is_uvm else 120)


@tool("sim.list_tests", "List tests from the regression manifest with tags/owners.",
      group="sim",
      schema={"type": "object", "properties": {"tag": {"type": "string"}}})
def sim_list_tests(ctx: ToolContext, tag: str = "") -> dict:
    tests = ctx.ws.tests_with_tag(tag) if tag else ctx.ws.tests
    return {"tests": [{"name": t.get("name"), "tags": t.get("tags", []),
                       "owner": t.get("owner")} for t in tests]}


@tool("sim.run", "Compile+run one test on the inner-loop simulator (async job). "
      "Waves are dumped and handed to the waveform service. UVM benches: pass "
      "uvm_test (or declare runner kind 'uvm' in the manifest) — the UVM report "
      "verdict overrides exit codes.", cost="metered",
      permission="submit", group="sim",
      schema={"type": "object", "properties": {
          "test": {"type": "string", "description": "test name or testbench top"},
          "seed": {"type": "integer"},
          "waves": {"type": "boolean", "default": True},
          "uvm_test": {"type": "string", "description": "+UVM_TESTNAME to run"},
          "detach": {"type": "boolean", "description":
                     "return immediately with a job id instead of blocking; "
                     "collect it later with job.wait or job.status. Use for "
                     "long runs so you can do other work meanwhile."},
          "plusargs": {"type": "array", "items": {"type": "string"}}},
          "required": ["test"]})
def sim_run(ctx: ToolContext, test: str, seed: int = 1, waves: bool = True,
            uvm_test: str = "", plusargs=None, detach: bool = False) -> dict:
    got = _plan_for_test(ctx, test, seed=seed, waves=waves,
                         uvm_test=uvm_test, plusargs=plusargs)
    if isinstance(got, dict):
        return got
    adapter, plan, files, timeout = got
    if detach:
        rec, _ = ctx.submit(plan, adapter, seed=seed, input_files=files,
                            timeout=timeout, detach=True)
        return {"job": rec.id, "status": "running", "detached": True,
                "seed": seed, "test": test,
                "note": "started in the background — call job.wait(job) when "
                        "you need the verdict, or job.status to peek. Nothing "
                        "about this test is known yet; do not report on it."}
    rec, res = ctx.submit(plan, adapter, seed=seed, input_files=files,
                          timeout=timeout)
    out = {"job": rec.id, "status": rec.status, "sim_status": res.status,
           "summary": res.summary, "seed": seed}
    # Cross-run history (FR-JOB-06). A reused verdict is not a second
    # observation of the same seed — recording it would manufacture agreement
    # and make a genuinely flaky test look stable.
    if not getattr(rec, "cached", False):
        from ..jobs.regression import normalize_signature
        sig = "" if rec.status == "passed" else normalize_signature(
            f"{res.summary} " + " ".join(d.message for d in res.diagnostics[:5]))
        ctx.history.record(test, seed, rec.status, signature=sig, job=rec.id)
    verdict = ctx.history.verdict(test, seed)
    if verdict.get("verdict") in ("flaky", "failing"):
        # the agent is about to debug this: knowing the same seed has already
        # disagreed with itself changes what the failure means
        out["history"] = verdict
    t = ctx.find_test(test)
    if _runner_of(t).get("kind") == "cocotb":
        out["runner"] = "cocotb"
    if res.metrics.get("uvm"):
        out["uvm"] = {k.replace("uvm_", ""): v for k, v in res.metrics.items()
                      if k.startswith("uvm_")}
    if res.artifacts.get("coverage_xml"):
        out["coverage_xml"] = ctx.rel(res.artifacts["coverage_xml"])
    if res.artifacts.get("waves"):
        out["waves_job"] = rec.id
    diags = [d.__dict__ for d in res.diagnostics if d.severity == "error"][:8]
    if diags:
        out["errors"] = diags
    return out


@tool("test.select", "Select tests affected by the current diff, via coverage "
      "attribution and module-name matching (SPEC FR-COV-03).", group="sim",
      schema={"type": "object", "properties": {
          "modules": {"type": "array", "items": {"type": "string"}}}})
def test_select(ctx: ToolContext, modules: list[str] | None = None) -> dict:
    modules = modules or []
    baseline = ctx.ws.coverage_baseline()
    selected = set()
    if baseline:
        for m in modules:
            for test, ids in baseline.model.per_test.items():
                if any(m in i for i in ids):
                    selected.add(test)
    # fall back / augment with name matching against the manifest
    for t in ctx.ws.tests:
        blob = " ".join([t.get("name", ""), *(t.get("tags") or [])])
        if any(m.split("_")[0] in blob for m in modules) or not modules:
            selected.add(t.get("name"))
    return {"modules": modules, "selected_tests": sorted(x for x in selected if x),
            "method": "coverage-attribution+name-match"}


@tool("regress.run", "Run a set of tests (by tag or explicit list), optionally "
      "sweeping seeds, farm/license-bounded, and cluster the failures.",
      cost="metered", permission="submit", group="sim",
      schema={"type": "object", "properties": {
          "tag": {"type": "string"}, "tests": {"type": "array", "items": {"type": "string"}},
          "seeds": {"type": "array", "items": {"type": "integer"}},
          "run_id": {"type": "string", "default": "reg"}}})
def regress_run(ctx: ToolContext, tag: str = "", tests=None, seeds=None,
                run_id: str = "reg") -> dict:
    names = tests or [t.get("name") for t in (ctx.ws.tests_with_tag(tag) if tag else ctx.ws.tests)]
    names = [n for n in names if n]
    if not names:
        return {"error": "no tests selected", "tag": tag}
    seeds = seeds or [1]

    def run_one(name, seed):
        got = _plan_for_test(ctx, name, seed=seed, waves=True)
        if isinstance(got, dict):
            raise RuntimeError(f"{name}: {got['error']}")
        adapter, plan, files, timeout = got
        rec, _ = ctx.submit(plan, adapter, seed=seed, input_files=files,
                            is_v3=True, timeout=timeout)
        return rec

    mgr = RegressionManager(run_one, max_parallel=self_parallel(ctx))
    res = mgr.run(names, seeds, run_id=run_id)
    return {"run_id": run_id, "summary": res.summary(),
            "passed": res.passed, "failed": res.failed, "total": res.total,
            "errored_tests": res.errored_tests,
            "clusters": [{"id": c.id, "count": c.count, "signature": c.signature,
                          "representative": c.representative} for c in res.clusters],
            "flaky": res.flaky}


def self_parallel(ctx: ToolContext) -> int:
    return int(ctx.ws.config.get("runner", {}).get("max_parallel", 4))


@tool("regress.failures", "Clustered failure signatures + representatives for a run.",
      group="sim",
      schema={"type": "object", "properties": {"run_id": {"type": "string"}}})
def regress_failures(ctx: ToolContext, run_id: str = "reg") -> dict:
    # rebuild clusters from the task's failed jobs (store-backed view)
    from ..jobs.regression import normalize_signature
    clusters: dict[str, dict] = {}
    for rec in ctx.task_jobs:
        if rec.kind == "sim" and rec.status not in ("passed",):
            sig = normalize_signature(rec.summary)
            c = clusters.setdefault(sig, {"id": f"C{len(clusters)+1}", "count": 0,
                                          "signature": sig, "jobs": []})
            c["count"] += 1
            c["jobs"].append(rec.id)
    return {"run_id": run_id, "clusters": list(clusters.values())}


def no_such_job(ctx: ToolContext, job: str) -> dict:
    """The teaching error every id-taking tool must share.

    job.log got this treatment first and its guessing loops stopped; repro
    did not, and a blind run burned ~10 calls enumerating invented job names
    against its mute "no job X" — one un-taught error path re-opens the whole
    failure class. One message, every tool."""
    recent = [r.id for r in ctx.runner.list_jobs()[-5:]]
    return {"error": f"no job '{job}' — job ids look like J-0249, not test "
                     f"names. Recent jobs: {', '.join(recent) or '(none)'}. "
                     f"A sim.run result carries its id in 'job'; job.list "
                     f"enumerates recent jobs."}


@tool("job.log", "Windowed grep over a job's logs (full logs never enter context).",
      group="sim",
      schema={"type": "object", "properties": {
          "job": {"type": "string"}, "pattern": {"type": "string"},
          "window": {"type": "integer", "default": 3}},
          "required": ["job", "pattern"]})
def job_log(ctx: ToolContext, job: str, pattern: str, window: int = 3) -> dict:
    # A miss must be distinguishable from a match-less grep. A live 4B passed
    # the TEST NAME here ("fifo_smoke") and got {"hits": []} — indistinguishable
    # from "the job exists and nothing matched" — so it believed it had
    # consulted a log that was never read. Same trap as the vacuous gates, one
    # layer down: an empty success for an action that touched nothing.
    if ctx.runner.get(job) is None:
        return no_such_job(ctx, job)
    hits = ctx.runner.grep_log(job, pattern, window=window)
    return truncate({"job": job, "pattern": pattern, "hits": hits}, max_items=20)


@tool("job.list", "Recent jobs, newest first: id, kind, status, summary. "
      "The way to FIND a job id — do not guess ids or pass test names.",
      group="sim",
      schema={"type": "object", "properties": {
          "limit": {"type": "integer", "default": 15},
          "kind": {"type": "string", "description":
                   "filter: sim | lint | synth | efpga-fabulous | …"}}})
def job_list(ctx: ToolContext, limit: int = 15, kind: str = "") -> dict:
    """A blind run proved the gap the hard way: the model KNEW records
    existed, had three tools demanding an id, and no way to enumerate one —
    it invented ten names in a row. The teaching errors list five recent ids,
    but an error is the wrong place to keep a directory."""
    recs = ctx.runner.list_jobs()
    if kind:
        recs = [r for r in recs if r.kind == kind]
    recs = recs[-max(1, min(int(limit or 15), 50)):]
    return {"jobs": [{"job": r.id, "kind": r.kind, "status": r.status,
                      "summary": (r.summary or "")[:60]}
                     for r in reversed(recs)]}


@tool("job.status", "Status and metrics of a job.", group="sim",
      schema={"type": "object", "properties": {"job": {"type": "string"}},
              "required": ["job"]})
def job_status(ctx: ToolContext, job: str) -> dict:
    rec = ctx.runner.get(job)
    if not rec:
        return no_such_job(ctx, job)
    return {"job": rec.id, "kind": rec.kind, "status": rec.status,
            "summary": rec.summary, "adapter": rec.adapter,
            "cpu_s": round(rec.cpu_seconds, 2), "artifacts": list(rec.artifacts)}


@tool("job.wait", "Block until a detached job finishes and return its verdict. "
      "Use after sim.run(detach=true). A timeout is not a failure — the job "
      "keeps running and you can wait again.", group="sim",
      schema={"type": "object", "properties": {
          "job": {"type": "string"},
          "timeout_s": {"type": "number", "default": 900}},
          "required": ["job"]})
def job_wait(ctx: ToolContext, job: str, timeout_s: float = 900) -> dict:
    rec = ctx.runner.wait(job, timeout=max(1.0, float(timeout_s)))
    if rec is None:
        return {"job": job, "status": "running", "timed_out": True,
                "note": "still running after the wait — it has NOT failed. "
                        "Do other work and wait again, or report what is known "
                        "so far without claiming anything about this job."}
    if job in ctx.pending_jobs:
        ctx.pending_jobs.remove(job)
    out = {"job": rec.id, "status": rec.status, "summary": rec.summary,
           "kind": rec.kind}
    res = rec.result or {}
    if res.get("status"):
        out["sim_status"] = res["status"]
    errs = [d for d in res.get("diagnostics", [])
            if d.get("severity") == "error"][:8]
    if errs:
        out["errors"] = errs
    if rec.artifacts.get("waves"):
        out["waves_job"] = rec.id
    return out


# ---- waveforms (group E) ----------------------------------------------------

@tool("wave.open", "Attach a run's waveform dump; returns time range, signal "
      "count and scope roots.", group="wave",
      schema={"type": "object", "properties": {"run": {"type": "string"}},
              "required": ["run"]})
def wave_open(ctx: ToolContext, run: str) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no waveform dump for run {run}"}
    return ws.info()


@tool("wave.find_signals", "Pattern-search signals in an open dump.", group="wave",
      schema={"type": "object", "properties": {
          "run": {"type": "string"}, "pattern": {"type": "string"}},
          "required": ["run", "pattern"]})
def wave_find_signals(ctx: ToolContext, run: str, pattern: str) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no dump for {run}"}
    return {"matches": ws.find_signals(pattern)}


@tool("wave.value", "Value of a signal at time t (dump time units).", group="wave",
      schema={"type": "object", "properties": {
          "run": {"type": "string"}, "signal": {"type": "string"},
          "time": {"type": "integer"}}, "required": ["run", "signal", "time"]})
def wave_value(ctx: ToolContext, run: str, signal: str, time: int) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no dump for {run}"}
    return {"signal": signal, "time": time, "value": ws.value(signal, time),
            "provenance": ws.provenance}


@tool("wave.changes", "Transitions of a signal in [t0,t1] (bounded).", group="wave",
      schema={"type": "object", "properties": {
          "run": {"type": "string"}, "signal": {"type": "string"},
          "t0": {"type": "integer"}, "t1": {"type": "integer"}},
          "required": ["run", "signal", "t0", "t1"]})
def wave_changes(ctx: ToolContext, run: str, signal: str, t0: int, t1: int) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no dump for {run}"}
    return truncate(ws.changes(signal, t0, t1))


@tool("wave.when", "First time an expression holds (e.g. \"vld && !rdy\"), "
      "searchable forward/backward.", group="wave",
      schema={"type": "object", "properties": {
          "run": {"type": "string"}, "expr": {"type": "string"},
          "from": {"type": "integer", "default": 0},
          "direction": {"type": "string", "enum": ["forward", "backward"], "default": "forward"}},
          "required": ["run", "expr"]})
def wave_when(ctx: ToolContext, run: str, expr: str, **kw) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no dump for {run}"}
    return ws.when(expr, from_t=kw.get("from", 0), direction=kw.get("direction", "forward"))


@tool("wave.compare", "First divergence between two runs over a scope (P1 triage).",
      group="wave",
      schema={"type": "object", "properties": {
          "run_a": {"type": "string"}, "run_b": {"type": "string"},
          "scope": {"type": "string", "default": ""}},
          "required": ["run_a", "run_b"]})
def wave_compare(ctx: ToolContext, run_a: str, run_b: str, scope: str = "") -> dict:
    a, b = ctx.resolve_wave(run_a), ctx.resolve_wave(run_b)
    if not a or not b:
        return {"error": "one or both dumps unavailable"}
    return a.compare(b, scope=scope)


@tool("wave.trace_x", "Earliest X/Z on a signal (seed for cone-based X-origin "
      "tracing).", group="wave",
      schema={"type": "object", "properties": {
          "run": {"type": "string"}, "signal": {"type": "string"}},
          "required": ["run", "signal"]})
def wave_trace_x(ctx: ToolContext, run: str, signal: str) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no dump for {run}"}
    res = ws.trace_x(signal)
    # combine with the design cone (X-origin candidates)
    if res.get("first_x_time") is not None:
        base = signal.split(".")[-1]
        mod = ctx.db.tops()[0] if ctx.db.tops() else ""
        cone = ctx.db.cone(mod, base, "fanin", 2) if mod else None
        if cone:
            res["cone_candidates"] = [s["name"] for s in cone.signals][:10]
    return res


@tool("wave.snapshot", "ASCII timing + WaveJSON excerpt for a set of signals in "
      "a window (for reports/evidence).", group="wave",
      schema={"type": "object", "properties": {
          "run": {"type": "string"},
          "signals": {"type": "array", "items": {"type": "string"}},
          "t0": {"type": "integer"}, "t1": {"type": "integer"}},
          "required": ["run", "signals", "t0", "t1"]})
def wave_snapshot(ctx: ToolContext, run: str, signals: list[str], t0: int, t1: int) -> dict:
    ws = ctx.resolve_wave(run)
    if not ws:
        return {"error": f"no dump for {run}"}
    snap = ws.snapshot(signals, t0, t1)
    return {"ascii": ascii_timing(snap), "wavejson": wavejson(snap),
            "provenance": ws.provenance}


# ---- coverage (group F) -----------------------------------------------------

@tool("cov.run", "Build+run a coverage sim for a test and merge into a named set.",
      cost="metered", permission="submit", group="cov",
      schema={"type": "object", "properties": {
          "test": {"type": "string"}, "set": {"type": "string", "default": "default"}},
          "required": ["test"]})
def cov_run(ctx: ToolContext, test: str, set: str = "default") -> dict:
    t = ctx.find_test(test)
    if _runner_of(t).get("kind") == "cocotb":
        # cocotb-coverage exports functional coverage XML from the Python TB —
        # running verilator line-coverage on a nonexistent SV top here used to
        # yield an empty model that read as 100%.
        got = _plan_for_test(ctx, test, waves=False)
        if isinstance(got, dict):
            return got
        adapter, plan, files, timeout = got
        rec, res = ctx.submit(plan, adapter, input_files=files, timeout=timeout)
        xml = res.artifacts.get("coverage_xml")
        if not xml:
            return {"job": rec.id, "status": rec.status,
                    "error": "no cocotb-coverage XML produced (does the TB "
                             "export coverage?)"}
        from ..coverage import ingest_cocotb_xml
        cov = ingest_cocotb_xml(xml, test=test)
        svc = ctx.coverage_sets.setdefault(set, CoverageService())
        svc.merge(cov)
        return {"job": rec.id, "set": set, "runner": "cocotb",
                "summary": svc.summary()}
    files, tb_top = _resolve_sim(ctx, test)
    adapter = ctx.registry.for_role("coverage")
    if not adapter or not adapter.available():
        return {"error": "no coverage adapter available"}
    plan = adapter.coverage(files, tb_top, ctx.ws.root)
    rec, res = ctx.submit(plan, adapter, input_files=files, timeout=180)
    if not res.artifacts.get("coverage"):
        return {"job": rec.id, "status": rec.status, "error": "no coverage.dat produced"}
    cov = ingest_verilator_dat(res.artifacts["coverage"], test=test)
    svc = ctx.coverage_sets.setdefault(set, CoverageService())
    svc.merge(cov)
    return {"job": rec.id, "set": set, "summary": svc.summary()}


@tool("cov.load", "Load a coverage file into a named set: UCIS XML (Questa/VCS/"
      "Xcelium export), cocotb-coverage XML, Verilator .dat, or functional "
      "JSON — format is sniffed. Functional holes/attribution/baseline gate "
      "then work over it.", group="cov",
      schema={"type": "object", "properties": {
          "path": {"type": "string"},
          "set": {"type": "string", "default": "default"},
          "test": {"type": "string", "description": "attribute hits to this test"}},
          "required": ["path"]})
def cov_load(ctx: ToolContext, path: str, set: str = "default", test: str = "") -> dict:
    from ..coverage import ingest_coverage
    p = path if os.path.isabs(path) else os.path.join(ctx.ws.root, path)
    if not os.path.exists(p):
        return {"error": f"no such file: {path}"}
    model, fmt = ingest_coverage(p, test=test)
    if not model.bins:
        return {"error": f"no coverage bins found in {path} (format: {fmt})"}
    svc = ctx.coverage_sets.setdefault(set, CoverageService())
    svc.merge(model)
    return {"set": set, "format": fmt, "bins_loaded": len(model.bins),
            "summary": svc.summary()}


@tool("cov.summary", "Coverage totals by kind/section for a set (deltas vs baseline).",
      group="cov",
      schema={"type": "object", "properties": {"set": {"type": "string", "default": "default"}}})
def cov_summary(ctx: ToolContext, set: str = "default") -> dict:
    svc = ctx.coverage_sets.get(set)
    if not svc:
        base = ctx.ws.coverage_baseline()
        if base:
            svc = base
        else:
            return {"error": f"no coverage set '{set}' and no baseline"}
    out = {"set": set, **svc.summary()}
    base = ctx.ws.coverage_baseline()
    if base:
        out["baseline_check"] = svc.baseline_check(base.model).__dict__
    return truncate(out)


@tool("cov.holes", "Unhit bins filtered by scope, mapped to source, with the "
      "nearest existing test (P5).", group="cov",
      schema={"type": "object", "properties": {
          "set": {"type": "string", "default": "default"},
          "scope": {"type": "string", "default": ""},
          "kind": {"type": "string", "default": "functional"},
          "limit": {"type": "integer", "default": 30}}})
def cov_holes(ctx: ToolContext, set: str = "default", scope: str = "",
              kind: str = "functional", limit: int = 30) -> dict:
    svc = ctx.coverage_sets.get(set) or ctx.ws.coverage_baseline()
    if not svc:
        return {"error": "no coverage set available"}
    return truncate(svc.holes(scope, kind, limit))


@tool("cov.attribution", "Which tests hit a bin, or which bins a test hits.",
      group="cov",
      schema={"type": "object", "properties": {
          "set": {"type": "string", "default": "default"},
          "bin": {"type": "string"}, "test": {"type": "string"}}})
def cov_attribution(ctx: ToolContext, set: str = "default", bin: str = "", test: str = "") -> dict:
    svc = ctx.coverage_sets.get(set) or ctx.ws.coverage_baseline()
    if not svc:
        return {"error": "no coverage set available"}
    return truncate(svc.attribution(bin_id=bin or None, test=test or None))


@tool("cov.exclude", "DRAFT a coverage exclusion with justification (approval-"
      "gated; a human must activate it — SPEC §11.4).", permission="approve", group="cov",
      schema={"type": "object", "properties": {
          "bin": {"type": "string"}, "justification": {"type": "string"}},
          "required": ["bin", "justification"]})
def cov_exclude(ctx: ToolContext, bin: str, justification: str) -> dict:
    return {"status": "proposed", "bin": bin, "justification": justification,
            "activated": False,
            "note": "exclusion drafted; requires human approval before it takes effect"}


# ---- formal (group G) -------------------------------------------------------

@tool("formal.run", "Bounded/unbounded property proof on a module carrying SVA.",
      cost="metered", permission="submit", group="formal",
      schema={"type": "object", "properties": {
          "module": {"type": "string"}, "depth": {"type": "integer", "default": 20},
          "mode": {"type": "string", "default": "prove"}},
          "required": ["module"]})
def formal_run(ctx: ToolContext, module: str, depth: int = 20, mode: str = "prove") -> dict:
    adapter = ctx.registry.for_role("formal")
    if not adapter or not adapter.available():
        return {"error": "no formal adapter available"}
    mod = ctx.db.module(module)
    files = [f for f in ctx.target.sources if mod and os.path.basename(mod.file) in f] \
        or ctx.target.sources
    plan = adapter.formal(files, module, workdir=os.path.join(str(ctx.ws.dot), "formal"),
                          mode=mode, depth=depth)
    rec, res = ctx.submit(plan, adapter, input_files=files, timeout=180)
    return {"job": rec.id, "status": rec.status, "formal_status": res.status,
            "summary": res.summary}
