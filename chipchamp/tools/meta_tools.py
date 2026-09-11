"""Meta tools (SPEC §9 group J): policy.check, evidence.bundle, report.done,
repro, doc.blockdiagram — the trust and reproducibility surface."""
from __future__ import annotations

import re
import time

from ..evidence import build_bundle, save_bundle
from ..policy import Evidence
from .base import tool, truncate
from .context import ToolContext


@tool("policy.check", "Which task class, verification ladder rung, gates and "
      "autonomy apply to the current change set — and any anti-gaming flags. "
      "Call before claiming work is done.", group="meta",
      schema={"type": "object", "properties": {
          "declare_nfc": {"type": "boolean", "description": "assert no functional change"},
          "declare_cdc": {"type": "boolean"},
          "declare_timing": {"type": "boolean"},
          "closure_claim": {"type": "boolean"}}})
def policy_check(ctx: ToolContext, declare_nfc=False, declare_cdc=False,
                 declare_timing=False, closure_claim=False) -> dict:
    ctx.declared_nfc = declare_nfc or ctx.declared_nfc
    ctx.declared_cdc = declare_cdc or ctx.declared_cdc
    ctx.declared_timing = declare_timing or ctx.declared_timing
    ctx.closure_claim = closure_claim or ctx.closure_claim
    diff = ctx.current_diff()
    chk = ctx.policy.check(diff, closure_claim=ctx.closure_claim)
    return truncate(chk.to_dict())


@tool("report.done", "The ONLY way to report task success. Validates every "
      "required gate against real job records; fails if any gate is missing or "
      "failing, or if an anti-gaming finding is unacknowledged (FR-CORE-02).",
      permission="submit", group="meta",
      schema={"type": "object", "properties": {
          "summary": {"type": "string"},
          "acknowledge": {"type": "array", "items": {"type": "string"},
                          "description": "anti-gaming finding kinds a human accepted"}}})
def report_done(ctx: ToolContext, summary: str = "", acknowledge=None) -> dict:
    diff = ctx.current_diff()
    ev = _collect_evidence(ctx, acknowledge or [])
    report = ctx.policy.validate_done(diff, ev, closure_claim=ctx.closure_claim)
    return {"accepted": report.all_passed,
            "task_class": report.task_class,
            "blocking_gates": report.blocking,
            "gate_table": [g.__dict__ for g in report.gates],
            "message": ("task verified — gates satisfied" if report.all_passed
                        else f"REJECTED: {len(report.blocking)} gate(s) not satisfied: "
                             + ", ".join(report.blocking))}


@tool("evidence.bundle", "Assemble + sign the evidence bundle for the current "
      "change set from job records (the model contributes only the narrative; "
      "cite only job ids job.list shows — unknown ids are refused).",
      permission="submit", group="meta",
      schema={"type": "object", "properties": {
          "bundle_id": {"type": "string"}, "narrative": {"type": "string"},
          "acknowledge": {"type": "array", "items": {"type": "string"}}}})
def evidence_bundle(ctx: ToolContext, bundle_id: str = "", narrative: str = "",
                    acknowledge=None) -> dict:
    # A narrative may only cite jobs the ledger knows. On one run the model
    # wrote a bundle around J-0005..J-0008 before any tool had run: nothing
    # checked the ids, the bundle was signed, and all_gates_passed came back
    # true because no gate had been evaluated — all-of-nothing. Both answers
    # were true statements about nothing, and the model read them as a pass.
    cited = sorted(set(re.findall(r"\bJ-\d{4}\b", narrative or "")))
    known = {r.id for r in ctx.runner.list_jobs()} | {r.id for r in ctx.task_jobs}
    unknown = [j for j in cited if j not in known]
    if unknown:
        return {"error": "narrative cites job ids that do not exist: " + ", ".join(unknown)
                + ". A bundle cites only jobs in the ledger — job.list shows them."}
    diff = ctx.current_diff()
    ev = _collect_evidence(ctx, acknowledge or [])
    report = ctx.policy.validate_done(diff, ev, closure_claim=ctx.closure_claim)
    bundle_id = bundle_id or f"B-{int(time.time())}"
    cov_delta = {}
    for svc in ctx.coverage_sets.values():
        base = ctx.ws.coverage_baseline()
        if base:
            cov_delta = {"summary": svc.baseline_check(base.model).summary}
        else:
            cov_delta = {"summary": f"total {svc.summary()['total']}% (no baseline)"}
        break
    b = build_bundle(
        bundle_id, report.task_class,
        change_summary={"files": [f.path for f in diff.files], "count": len(diff.files)},
        semantic_diff=_semantic_diff(ctx),
        gate_report=report, jobs=ctx.task_jobs, coverage_delta=cov_delta,
        repro_commands=_repro_all(ctx),
        antigaming=ev.antigaming, narrative=narrative)
    path = save_bundle(b, str(ctx.ws.dot / "bundles"))
    out = {"bundle_id": bundle_id, "path": ctx.rel(path), "signature": b.signature,
           "gates_evaluated": len(report.gates),
           "all_gates_passed": report.all_passed if report.gates else None,
           "verify": b.verify()}
    if not report.gates:
        out["warning"] = ("no gate has been evaluated for this task — nothing has run; "
                          "this bundle certifies no result")
    return out


@tool("mr.prepare", "Prepare a merge request for the current change set: create "
      "a branch, commit the tracked edits, and write an MR description with the "
      "gate table + evidence bundle. Requires autonomy L2 on every touched path "
      "(SPEC §11.2); pushing/merging stays with the human.",
      permission="submit", group="meta",
      schema={"type": "object", "properties": {
          "branch": {"type": "string"},
          "title": {"type": "string"},
          "narrative": {"type": "string"},
          "acknowledge": {"type": "array", "items": {"type": "string"}}},
          "required": ["branch", "title"]})
def mr_prepare(ctx: ToolContext, branch: str, title: str, narrative: str = "",
               acknowledge=None) -> dict:
    import subprocess

    from ..policy.autonomy import decide
    diff = ctx.current_diff()
    if not diff.files:
        return {"error": "no tracked edits in this task — nothing to put in an MR"}
    tclass = ctx.policy.check(diff).task_class
    decision = decide(tclass, [f.path for f in diff.files],
                      ctx.policy.autonomy_rules, ctx.policy.default_autonomy)
    if not decision.may_open_mr:
        return {"error": f"autonomy ceiling: effective level {decision.level} for "
                         f"these paths — L2 required to open an MR. A human must "
                         f"apply this change.", "autonomy": decision.__dict__}
    # gates must be evaluated (not necessarily all passing — the MR shows them)
    ev = _collect_evidence(ctx, acknowledge or [])
    report = ctx.policy.validate_done(diff, ev, closure_claim=ctx.closure_claim)

    def git(*args):
        return subprocess.run(["git", "-C", ctx.ws.root, *args],
                              capture_output=True, text=True, timeout=30)

    if git("rev-parse", "--git-dir").returncode != 0:
        return {"error": "not a git repository — cannot prepare an MR"}
    cur = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if git("checkout", "-b", branch).returncode != 0:
        return {"error": f"could not create branch '{branch}' (exists?)"}
    try:
        for path in ctx.edits:
            git("add", path)
        msg = f"{title}\n\n[chipchamp] task class: {report.task_class}; " \
              f"gates: {'ALL PASS' if report.all_passed else 'INCOMPLETE ' + str(report.blocking)}"
        commit = git("-c", "user.email=chipchamp-agent@local",
                     "-c", "user.name=chipchamp agent", "commit", "-m", msg)
        if commit.returncode != 0:
            git("checkout", cur)
            return {"error": f"commit failed: {commit.stderr.strip()[:200]}"}
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    finally:
        git("checkout", cur)

    b = evidence_bundle(ctx, bundle_id=f"B-mr-{branch.replace('/', '-')}",
                        narrative=narrative or title, acknowledge=acknowledge)
    mr_dir = ctx.ws.dot / "mr"
    mr_dir.mkdir(parents=True, exist_ok=True)
    body = [f"# {title}", "",
            f"Branch: `{branch}` @ `{sha}` · task class: **{report.task_class}** · "
            f"gates: {'✅ all passed' if report.all_passed else '❌ ' + ', '.join(report.blocking)}",
            "", "## Gate table",
            "| gate | status | evidence |", "|---|---|---|"]
    body += [f"| {g.name} | {g.status} | {g.evidence} |" for g in report.gates]
    body += ["", f"Evidence bundle: `{b['path']}` (signature `{b['signature'][:24]}…`)",
             "", "## Files", *[f"- `{p}`" for p in sorted(ctx.edits)]]
    mr_path = mr_dir / f"{branch.replace('/', '-')}.md"
    mr_path.write_text("\n".join(body))
    return {"branch": branch, "commit": sha, "mr_body": ctx.rel(str(mr_path)),
            "gates_all_passed": report.all_passed, "bundle": b["path"],
            "note": "branch created locally; push/review/merge remain human actions"}


@tool("caps", "What this machine can actually run: per-role adapter selection "
      "(sim/lint/synth/formal/sta/pnr/efpga-fabulous/...) with availability, plus every "
      "adapter's version, languages and known gaps, and the active SV front end. "
      "Call before planning work that depends on a backend, instead of failing "
      "into a missing tool.", cost="cheap", group="meta",
      schema={"type": "object", "properties": {
          "role": {"type": "string", "description":
                   "only this role, e.g. sim|lint|synth|formal|sta|pnr|efpga-fabulous"},
          "available_only": {"type": "boolean", "description":
                             "hide adapters that are not installed"}}})
def caps(ctx: ToolContext, role: str = "", available_only: bool = False) -> dict:
    from ..design import slang_frontend
    mans = ctx.registry.all_manifests()
    roles = ctx.registry.role_report()
    if role:
        mans = [m for m in mans if role in m["roles"]]
        roles = {r: v for r, v in roles.items() if r == role}
        if not mans and not roles:
            return {"error": f"unknown role '{role}'; roles: "
                             + ", ".join(ctx.registry.role_report())}
    unavailable = sorted(m["adapter"] for m in mans if not m["available"])
    if available_only:
        mans = [m for m in mans if m["available"]]
    try:
        from ..skills import discover as _sk_discover
        from ..skills import skill_dirs as _sk_dirs
        skills_info = {"count": len(_sk_discover(ctx.ws)),
                       "dirs": [f"{p} ({src})" for p, src in _sk_dirs(ctx.ws)],
                       "index": ctx.ws.skills_index_mode()}
    except Exception:
        skills_info = {"count": 0, "dirs": [], "index": "compact"}
    return truncate({
        "roles": roles,
        "adapters": mans,
        "unavailable": unavailable,
        "sv_frontend": "slang" if slang_frontend.available() else "pragmatic",
        "skills": skills_info,
        "note": "unavailable adapters are integration glue awaiting the tool "
                "install (commercial ones validate against fixtures); a role "
                "with available=false has no live backend on this machine",
    })


@tool("repro", "Print the exact reproduction command(s) for a past job.",
      group="meta",
      schema={"type": "object", "properties": {"job": {"type": "string"}},
              "required": ["job"]})
def repro(ctx: ToolContext, job: str) -> dict:
    cmd = ctx.runner.repro_command(job)
    if cmd:
        return {"job": job, "repro": cmd}
    from .verif_tools import no_such_job
    return no_such_job(ctx, job)


@tool("doc.blockdiagram", "ASCII block diagram of a hierarchy scope.", group="meta",
      schema={"type": "object", "properties": {
          "top": {"type": "string"}, "depth": {"type": "integer", "default": 2}}})
def doc_blockdiagram(ctx: ToolContext, top: str = "", depth: int = 2) -> dict:
    db = ctx.db
    top = top or (db.tops()[0] if db.tops() else "")
    if not top:
        return {"error": "no top"}
    node = db.elaborate(top)
    lines = []

    def walk(n, ind):
        params = ",".join(f"{k}={v}" for k, v in list(n.params.items())[:3])
        lines.append("  " * ind + f"┌─ {n.inst_name or n.module} : {n.module}"
                     + (f" #({params})" if params else ""))
        if ind < depth:
            for c in n.children:
                walk(c, ind + 1)
    walk(node, 0)
    return {"top": top, "diagram": "\n".join(lines)}


# ---- helpers ----------------------------------------------------------------


def _collect_evidence(ctx: ToolContext, acknowledge: list[str]) -> Evidence:
    ev = Evidence(jobs=list(ctx.task_jobs))
    ev.root_cause_notes = [n for n in getattr(ctx, "task_notes", [])
                           if n.get("kind") == "root_cause"]
    ev.riscv_cosim = getattr(ctx, "riscv_cosim", None)
    ev.riscv_compliance = getattr(ctx, "riscv_compliance", None)
    # fpga_fits headroom budget: [fpga] max_utilization (default 1.0 = must fit)
    try:
        budget = (ctx.ws.config.get("fpga", {}) or {}).get("max_utilization")
        if budget is not None:
            ev.fpga_max_utilization = float(budget)
    except (TypeError, ValueError):
        pass
    # coverage baseline verdict
    base = ctx.ws.coverage_baseline()
    if base and ctx.coverage_sets:
        svc = next(iter(ctx.coverage_sets.values()))
        ev.coverage_baseline = svc.baseline_check(base.model)
    elif base is None and ctx.coverage_sets:
        # no baseline on disk: treat current as its own baseline (non-regressing)
        svc = next(iter(ctx.coverage_sets.values()))
        ev.coverage_baseline = svc.baseline_check(svc.model)
    # cdc: count crossings across touched modules as "new" unless declared clean
    if ctx.declared_cdc:
        cdc = 0
        for m in ctx.db.modules:
            dm = ctx.db.domain_report(m)
            if dm:
                cdc += len(dm.crossings)
        ev.cdc_new_violations = cdc
    # timing gate: sta.run with a baseline computed the delta (SPEC §11.1)
    if ctx.sta_delta is not None:
        ev.sta_new_violations = ctx.sta_delta.get("new_violations")
    # regmap gate: regenerate + byte-compare against the source of truth
    from .regmap_tools import regen_check
    ev.regmap_regenerated = regen_check(ctx)
    # anti-gaming acknowledgements come from a human via the CLI/agent operator
    ev.antigaming = [{"kind": k, "file": "", "detail": "", "acknowledged": True}
                     for k in acknowledge]
    return ev


def _semantic_diff(ctx: ToolContext) -> dict:
    # best-effort: report which touched modules changed ports/params vs the
    # pre-edit text by reparsing old vs new
    from ..design.parser import parse_source
    changes = {"port_changes": [], "param_changes": []}
    for path, e in ctx.edits.items():
        if not path.endswith((".sv", ".v")):
            continue
        try:
            old_mods = {m.name: m for m in parse_source(e["old"], path)} if e["old"] else {}
            new_mods = {m.name: m for m in parse_source(e["new"], path)}
        except Exception:
            continue
        for name in new_mods.keys() & old_mods.keys():
            op = {p.name for p in old_mods[name].ports}
            np = {p.name for p in new_mods[name].ports}
            for pn in np ^ op:
                changes["port_changes"].append(
                    {"module": name, "port": pn,
                     "change": "added" if pn in np else "removed"})
    return changes


def _repro_all(ctx: ToolContext) -> list[str]:
    cmds = []
    for rec in ctx.task_jobs:
        c = ctx.runner.repro_command(rec.id)
        if c:
            cmds.append(c)
    return cmds


@tool("tools.load", "Load the schemas of deferred tool groups so you can call "
      "them. Your tool list starts at a core set; everything else is one call "
      "away. Pass group names (e.g. wave, physical, formal, riscv, cov).",
      group="meta",
      schema={"type": "object", "properties": {
          "groups": {"type": "array", "items": {"type": "string"},
                     "description": "group names to load"}},
          "required": ["groups"]})
def tools_load(ctx: ToolContext, groups=None) -> dict:
    """Reaches back into the loop because the live tool set is the loop's, not
    the context's — the context deliberately knows nothing about the model."""
    loop = getattr(ctx, "loop", None)
    if loop is None or not getattr(loop, "disclose", False):
        return {"note": "all tools are already loaded", "loaded": []}
    return loop.load_groups(list(groups or []))


# The subagent roles belong in agent.spawn's SCHEMA, not only in its error
# message: a list the model can only discover by guessing wrong first is no
# help to a model that never retries. It cannot be resolved here, though —
# agent.roles imports this package, so importing it back at module load is a
# cycle that silently yields an empty enum. `catalog.all_tools()` fills it once
# the cycle has settled; this placeholder is what it fills.
ROLE_NAMES: list[str] = []


@tool("agent.spawn", "Fan out typed subagents to work in parallel, then get "
      "their findings back. Use when the work splits cleanly — one per failing "
      "test, per module to lint-fix, per review lens. Each subagent gets a "
      "least-privilege tool subset and shares this session's job store and "
      "license pool, so a wide fan-out cannot check out more seats than exist. "
      "Only YOU can report the task done.",
      permission="submit", group="meta",
      schema={"type": "object", "properties": {
          "tasks": {"type": "array", "items": {"type": "object", "properties": {
              # The enum is load-bearing, not decoration. A live run had a model
              # pass role="design"/"verification" — the LABELS from the request —
              # and the call was rejected. With the roles enumerated a
              # constrained decoder cannot emit an invalid one, and a model
              # reading the schema no longer has to guess the vocabulary.
              "role": {"type": "string", "enum": ROLE_NAMES,
                       "description": "which kind of subagent (not a label)"},
              "prompt": {"type": "string",
                         "description": "the assignment, self-contained"},
              "label": {"type": "string",
                        "description": "your name for this one, e.g. 'design'"}},
              "required": ["role", "prompt"]}},
          "max_concurrent": {"type": "integer", "default": 4}},
          "required": ["tasks"]})
def agent_spawn(ctx: ToolContext, tasks=None, max_concurrent: int = 4) -> dict:
    """The Orchestrator has existed since §8.10 and was reachable only from
    `triage --agents`; the model had no way to fan out at all. Hardware
    parallelism is abundant — per-module, per-seed, per-corner, per-lens — and
    the hard part (one shared license pool, so 40 subagents do not become 40
    seats) was already solved.
    """
    from ..agent.orchestrator import Orchestrator, SubagentTask
    from ..agent.roles import builtin_roles
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    if not tasks:
        return {"error": "no tasks given"}
    roles = builtin_roles()
    # A MISSING role is the common failure, not a misspelt one: a live gpt-oss
    # run invented its own task shape ({tool, args}) with no `role` at all. The
    # first version put None into this set and died in the join with a bare
    # TypeError — so the model was told nothing useful and retried the same
    # broken shape. Name what is wrong, and always ship the catalogue.
    bad = sorted({str(t.get("role") or "(missing)") for t in tasks
                  if t.get("role") not in roles})
    if bad:
        return {"error": f"unknown or missing role(s): {', '.join(bad)}. "
                         f"Each task needs role + prompt; `role` must be one of "
                         f"the names below and is NOT a free-text label.",
                "roles": {n: r.description for n, r in roles.items()},
                "example": {"tasks": [{"role": "reviewer", "label": "design",
                                       "prompt": "Review sync_fifo for …"}]}}
    empty = [t.get("label") or t["role"] for t in tasks if not t.get("prompt")]
    if empty:
        return {"error": f"these tasks have no prompt: {', '.join(empty)}. "
                         f"A subagent starts with no context beyond what you "
                         f"write, so each prompt must be self-contained."}
    # A subagent that could spawn is a fork bomb with a license pool attached.
    # Depth is capped at one by construction: only the parent context carries a
    # loop, and a child's tool set comes from its Role, which has no agent.*.
    if getattr(ctx, "is_subagent", False):
        return {"error": "subagents cannot spawn subagents — do the work or "
                         "report back to the orchestrator"}
    if len(tasks) > 16:
        return {"error": f"{len(tasks)} is too many at once; split the work into "
                         f"rounds of 16 or fewer"}
    loop = getattr(ctx, "loop", None)
    gateway = getattr(ctx, "gateway", None)
    if gateway is None:
        return {"error": "no model available to run subagents"}

    def factory(role_name: str):
        """Route each role to its own model when a router is configured — a
        wave-analyst does not need what an RTL author needs (§14)."""
        router = getattr(loop, "router", None)
        if router is not None and getattr(router.cfg, "enabled", False):
            try:
                ref = router.pick(_ROLE_NEED.get(role_name, "planner"))
                if ref and ref != gateway.ref:
                    gw = router.build_gateway(ref, like=gateway)
                    if gw is not None and gw.available:
                        return gw
            except Exception:
                pass    # routing is an optimization; never block the fan-out
        return gateway

    def _mark(child):
        child.is_subagent = True
        return child

    orch = Orchestrator(ctx, factory, max_concurrent=max(1, int(max_concurrent)),
                        on_event=getattr(loop, "on_event", None))
    _wrap_children(orch, _mark)
    results = orch.run([SubagentTask(role=t["role"], prompt=t.get("prompt", ""),
                                     label=t.get("label") or t["role"])
                        for t in tasks])
    return truncate({
        "subagents": [{"label": r.label, "role": r.role, "steps": r.steps,
                       "jobs": r.jobs, "edited": r.edited,
                       "error": r.error, "findings": r.text} for r in results],
        "note": "their job records and edits are now yours; validate with "
                "policy.check / report.done as usual"}, max_items=16)


# which capability each role should be routed to when a router is configured
_ROLE_NEED = {"triage-analyst": "debugger", "wave-analyst": "debugger",
              "lint-fixer": "rtl_author", "test-writer": "rtl_author",
              "reviewer": "planner", "web-researcher": "planner"}


def _wrap_children(orch, mark):
    """Tag every child context as a subagent, so the recursion guard holds."""
    inner = orch._child_context

    def child():
        return mark(inner())

    orch._child_context = child


@tool("note.add", "Record a durable finding for future sessions: a root cause "
      "and its mechanism, a fix pattern that worked, a hardware constraint, a "
      "decision and its reason, a gotcha. Only things NOT derivable from the "
      "tree — the test is whether a competent engineer joining tomorrow would "
      "want it on the whiteboard. Cite a job id or file:line as evidence.",
      group="meta",
      schema={"type": "object", "properties": {
          "kind": {"type": "string",
                   "enum": ["root_cause", "fix_pattern", "constraint",
                            "decision", "gotcha"]},
          "subject": {"type": "string", "description": "short, specific"},
          "detail": {"type": "string"},
          "evidence": {"type": "string", "description": "job id or file:line"}},
          "required": ["kind", "subject", "detail"]})
def note_add(ctx: ToolContext, kind: str, subject: str, detail: str,
             evidence: str = "") -> dict:
    out = ctx.notebook.add(kind, subject, detail, evidence=evidence,
                           source="agent")
    ctx.task_notes.append({"id": out.get("id", ""), "kind": kind,
                           "subject": subject, "detail": detail,
                           "evidence": evidence})
    return out


@tool("note.list", "What this project has already learned (the notebook).",
      group="meta",
      schema={"type": "object", "properties": {
          "kind": {"type": "string"}}})
def note_list(ctx: ToolContext, kind: str = "") -> dict:
    return truncate({"notes": ctx.notebook.entries(kind)}, max_items=30)


@tool("checkpoint.restore", "Restore the workspace files captured at a GREEN "
      "verification job. Checkpoints are taken automatically whenever a "
      "lint/sim job passes; restoring puts every captured file back exactly "
      "as it was at that green state — the escape hatch when edits have "
      "tangled past repair.", permission="write", group="workspace",
      schema={"type": "object", "properties": {
          "job": {"type": "string",
                  "description": "the green job id, e.g. J-0004"}},
          "required": ["job"]})
def checkpoint_restore(ctx: ToolContext, job: str) -> dict:
    import json as _json
    import os
    import shutil
    base = os.path.join(str(ctx.ws.dot), "checkpoints")
    d = os.path.join(base, job)
    if not os.path.isdir(d):
        avail = sorted(os.listdir(base)) if os.path.isdir(base) else []
        return {"error": f"no checkpoint for '{job}'."
                + (f" Available green checkpoints: {', '.join(avail)}."
                   if avail else " None recorded yet — a checkpoint appears "
                   "automatically the first time a lint/sim job passes.")}
    try:
        manifest = _json.load(open(os.path.join(d, "manifest.json")))
    except (OSError, ValueError):
        return {"error": f"checkpoint '{job}' is unreadable"}
    restored = []
    for rel in manifest.get("files", []):
        src = os.path.join(d, "files", rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(ctx.ws.root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        old = open(dst, errors="replace").read() if os.path.exists(dst) else ""
        new = open(src, errors="replace").read()
        if old != new:
            shutil.copy2(src, dst)
            ctx.record_edit(rel, old, new, "restored")
            restored.append(rel)
    ctx.ws.invalidate(ctx.target_name)
    return {"job": job, "restored": restored,
            "unchanged": len(manifest.get("files", [])) - len(restored),
            "note": "files are back at the green state; make MINIMAL changes "
                    "from here and re-run the check after each one"}
