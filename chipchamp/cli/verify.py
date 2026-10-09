"""chipchamp CLI — verify commands (split from the one-file cli.py)."""
from __future__ import annotations

import json

import click

from ..tools import catalog_summary

try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _console, _ctx, _emit, _run_tool, cli
from .model import _build_gateway, _build_router
from .repl import _agent_events, _context_budget


def _parse_seeds(value):
    if value is None:
        return None
    try:
        if not value.strip() or any(not s.strip() for s in value.split(",")):
            raise ValueError
        return [int(s.strip()) for s in value.split(",")]
    except ValueError:
        raise click.BadParameter("use comma-separated integers, e.g. 1,2,3", param_hint="--seeds")


@cli.command("verify-plan")
@click.option("--changed", "changed_paths", multiple=True, help="changed file (repeatable); default: session/Git changes")
@click.option("--base", default="HEAD", help="Git revision to compare against")
@click.option("--seeds", default=None, help="comma-separated seeds; default: tests.yaml")
@click.option("--out", default="", help="save a workspace-relative .json plan for regress --plan")
@click.pass_context
def verify_plan_cmd(ctx, changed_paths, base, seeds, out):
    """Explain affected tests, required gates and regression budget without running jobs."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    result = _run_tool(c, "verify.plan", changed_paths=list(changed_paths) or None,
                       base=base, seeds=_parse_seeds(seeds))
    if out and "error" not in result:
        from ..tools.paths import ws_path, PathRefused
        from ..util.jsonio import atomic_write, dump_json
        try:
            path, relative = ws_path(c, out, write=True)
            if not relative.endswith(".json"):
                raise ValueError("--out must name a workspace-relative .json file")
            atomic_write(path, dump_json(result))
            result["saved_plan"] = relative
        except PathRefused as e:
            result = e.payload
        except (ValueError, OSError) as e:
            result = {"error": str(e)}
    if ctx.obj["as_json"]:
        _emit(result, True)
    elif "error" in result:
        echo(f"[red]{result['error']}[/]")
    else:
        selection = result["selection"]
        echo(f"[bold]{result['plan_id']}[/] — {result['target']}: {selection['mode']}")
        echo("Changed modules: " + (", ".join(selection["changed_modules"]) or "none identified"))
        echo("Required gates: " + (", ".join(result["required_gates"]) or "none for this change"))
        for name in selection["selected_tests"]:
            echo(f"  {name}: {'; '.join(selection['reasons'][name])}")
        echo(f"{result['test_jobs']} test/seed jobs selected; "
             f"{result['declared_suite_jobs']} in the declared suite; "
             f"{result['budget']['v3_remaining']} V3 submissions remain.")
        if not result["budget"]["within_budget"]:
            echo("[yellow]The selected run exceeds the remaining V3 budget.[/]")
        for warning in selection["warnings"]:
            echo(f"[yellow]{warning}[/]")
        echo(result["note"])
        if result.get("saved_plan"):
            echo(f"Saved: {result['saved_plan']}")
    if "error" in result:
        raise SystemExit(1)


@cli.command("regress")
@click.option("--test", "tests", multiple=True, help="test name (repeatable)")
@click.option("--tag", default="", help="select tests with this tag")
@click.option("--seeds", default=None, help="comma-separated seeds; default: tests.yaml")
@click.option("--affected", is_flag=True, help="select tests using changed-module impact")
@click.option("--changed", "changed_paths", multiple=True, help="changed file for --affected (repeatable)")
@click.option("--base", default="HEAD", help="Git revision for --affected")
@click.option("--junit", default="", help="workspace-relative .xml export for CI")
@click.option("--sarif", default="", help="workspace-relative .sarif diagnostic export for CI")
@click.option("--plan", "plan_file", default="", help="run a saved verification plan")
@click.option("--resume", default="", help="resume a previous run's checkpoint.json")
@click.option("--run-id", default="reg", help="label for this regression")
@click.pass_context
def regress_cmd(ctx, tests, tag, seeds, affected, changed_paths, base, junit, sarif, plan_file, resume, run_id):
    """Run regressions and save JSON/JUnit reports. Failures/errors exit nonzero."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    result = _run_tool(c, "regress.run", tests=list(tests) or None, tag=tag,
                       seeds=_parse_seeds(seeds), affected=affected,
                       changed_paths=list(changed_paths) or None, base=base,
                       junit=junit, sarif=sarif, plan_file=plan_file, resume=resume, run_id=run_id)
    if ctx.obj["as_json"]:
        _emit(result, True)
    else:
        echo(result.get("summary", result.get("error", result.get("note", "regression failed"))))
        if result.get("summary") and result.get("error"):
            echo(f"[red]{result['error']}[/]")
        for kind, path in result.get("reports", {}).items():
            echo(f"  {kind}: {path}")
        if result.get("checkpoint"):
            echo(f"  resume: chipchamp regress --resume {result['checkpoint']}")
    if result.get("error") or result.get("denied") or result.get("failed") or result.get("errored"):
        raise SystemExit(1)


@cli.command()
@click.argument("module")
@click.option("--out", "out_dir", default="", help="bench dir (default verif/uvm/<module>)")
@click.option("--no-test", is_flag=True, help="don't register in tests.yaml")
@click.pass_context
def uvm(ctx, module, out_dir, no_test):
    """Scaffold a runnable UVM bench for MODULE (agent+env+test+tb, filelist,
    manifest entry). Runs license-free on Verilator 5 + Accellera uvm-core."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "uvm.scaffold", module=module, out_dir=out_dir,
                                            add_test=not no_test)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        raise SystemExit(1)
    echo(f"[green]UVM bench generated[/] in {r['bench_dir']} "
         f"({len(r['files'])} files)")
    for f in r["files"]:
        echo(f"  {f['path']}")
    echo(f"  manifest: {r['manifest']}")
    args = r["run_with"]["args"]
    echo(f"  run: chipchamp sim {args['test']}   [dim](or sim.run in a session)[/]")
    echo(f"  [dim]{r['note']}[/]")


@cli.command()
@click.argument("top", default="")
@click.pass_context
def synth(ctx, top):
    """Synthesis snapshot (cells/area + inferred-latch warnings)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "synth.run", top=top)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    echo(f"{r.get('job')}: {r.get('summary')}  metrics={r.get('metrics')}")
    for d in r.get("warnings", []):
        echo(f"  [yellow]{d['code']}[/]: {d['message'][:90]}")


@cli.command()
@click.argument("test")
@click.option("--set", "cset", default="default")
@click.pass_context
def cov(ctx, test, cset):
    """Run a coverage sim and summarize."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "cov.run", test=test, set=cset)
    _emit(r, ctx.obj["as_json"])


@cli.command()
@click.option("--tag", default="smoke")
@click.option("--seeds", default="1,2,3")
@click.option("--agents", is_flag=True,
              help="fan out one model-driven triage-analyst subagent per cluster (§8.10)")
@click.pass_context
def triage(ctx, tag, seeds, agents):
    """Run tests, cluster failures, and gather root-cause evidence (P1)."""
    from ..agent.playbooks import run_triage, run_triage_fanout
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    seed_list = [int(s) for s in seeds.split(",") if s.strip()]
    if agents:
        gw, reason, _ = _build_gateway(c)
        if gw is None:
            echo(f"[yellow]--agents needs a model:[/] {reason}; falling back to scripted triage")
            r = run_triage(c, tag=tag, seeds=seed_list)
        else:
            # subagents may be routed to a different (cheaper) model (§14)
            sub_gw, _, _ = _build_gateway(c, role="subagent")
            sub_gw = sub_gw or gw
            echo(f"[dim]fan-out model: {sub_gw.ref}[/]")
            r = run_triage_fanout(c, lambda role: sub_gw, tag=tag, seeds=seed_list)
    else:
        r = run_triage(c, tag=tag, seeds=seed_list)
    if ctx.obj["as_json"]:
        _emit(r, True)
        return
    if "error" in r:
        echo(f"[red]{r['error']}[/]")
        return
    echo(f"[bold]{r['summary']}[/]")
    for cl in r["clusters"]:
        echo(f"\n[red]● {cl['id']}[/] ×{cl['count']} — {cl['signature'][:90]}")
        rep = cl.get("representative") or {}
        echo(f"  representative: {rep.get('test')} seed={rep.get('seed')} job={rep.get('job')}")
        ev = cl.get("evidence", {})
        if ev.get("first_divergence"):
            d = ev["first_divergence"]
            echo(f"  first divergence: [cyan]{d['signal']}[/] @ {d['time']} "
                 f"(fail={d['value_a']} vs pass={d['value_b']})")
        for line in ev.get("cone", [])[:3]:
            echo(f"    [dim]{line.splitlines()[-1]}[/]")
        for s in ev.get("suspects", [])[:2]:
            echo(f"  suspect: {s['commit']} {s.get('subject','')[:50]}")
        if cl.get("analysis"):
            a = cl["analysis"]
            echo(f"  [bold]analyst ({a['steps']} steps):[/] {a['text'][:400]}")
    for fl in r.get("flaky", []):
        echo(f"[yellow]flaky[/]: {fl['test']} pass={fl['pass_seeds']} fail={fl['fail_seeds']}")


@cli.command("lint-burndown")
@click.pass_context
def lint_burndown(ctx):
    """Snapshot lint, format, re-lint, report the delta (P6)."""
    from ..agent.playbooks import run_lint_burndown
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _emit(run_lint_burndown(c), ctx.obj["as_json"])


# ---- jobs / repro / adapters / tools ---------------------------------------

@cli.command()
@click.pass_context
def jobs(ctx):
    """List recorded jobs."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    recs = c.runner.list_jobs()
    if ctx.obj["as_json"]:
        _emit([r.to_dict() for r in recs], True)
        return
    for r in recs[-25:]:
        col = {"passed": "green", "failed": "red", "error": "red"}.get(r.status, "yellow")
        echo(f"{r.id}  [{col}]{r.status:8}[/] {r.kind:9} {r.adapter:10} {r.summary[:50]}")


@cli.command()
@click.argument("job")
@click.pass_context
def repro(ctx, job):
    """Print the reproduction command for a job."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    r = _run_tool(c, "repro", job=job)
    echo(r.get("repro", r.get("error")))


@cli.command()
@click.pass_context
def adapters(ctx):
    """Show EDA adapter capability manifests and availability."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    mans = c.registry.all_manifests()
    roles = c.registry.role_report()
    if ctx.obj["as_json"]:
        _emit({"adapters": mans, "roles": roles}, True)
        return
    if _console():
        t = Table("adapter", "avail", "roles", "version")
        for m in mans:
            t.add_row(m["adapter"], "✓" if m["available"] else "✗",
                      ",".join(m["roles"]), m["version"][:40])
        _console().print(t)
        rt = Table("role", "selected", "live", "pinned")
        for role, v in roles.items():
            rt.add_row(role, v["selected"] or "—",
                       "✓" if v["available"] else "[red]✗[/]",
                       v["pinned"] or "")
        _console().print(rt)
    else:
        for m in mans:
            echo(f"{m['adapter']:12} {'ok' if m['available'] else '--'} {m['roles']}")
        for role, v in roles.items():
            echo(f"role {role:10} -> {v['selected'] or '-'} "
                 f"{'ok' if v['available'] else 'MISSING'}")


@cli.command()
@click.pass_context
def tools(ctx):
    """List the agent tool catalog."""
    if ctx.obj["as_json"]:
        _emit(catalog_summary(), True)
        return
    group = None
    for t in catalog_summary():
        if t["group"] != group:
            group = t["group"]
            echo(f"\n[bold]{group}[/]")
        echo(f"  {t['name']:22} [dim]{t['cost']}/{t['permission']}[/]  {t['description'][:70]}")


# ---- agent ------------------------------------------------------------------


@cli.command()
@click.argument("spec")
@click.option("--validate-only", is_flag=True)
@click.pass_context
def regmap(ctx, spec, validate_only):
    """Validate/regenerate register collateral from a YAML spec (P10)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    from ..tools import all_tools as _t
    v = _t()["regmap.validate"].handler(c, spec=spec)
    if not v.get("ok"):
        echo(f"[red]spec invalid:[/] {v.get('errors', v.get('error'))}")
        raise SystemExit(1)
    echo(f"[green]spec ok[/] — {', '.join(v['registers'])}")
    if validate_only:
        return
    g = _t()["regmap.generate"].handler(c, spec=spec)
    for w in g.get("written", []):
        mark = "regenerated" if w["changed"] else "unchanged"
        echo(f"  {w['path']}  [dim]{mark} ({w['sha256']})[/]")


@cli.command()
@click.option("--suite", default="b1", type=click.Choice(["b1", "b6", "all"]))
@click.option("--mutants", default=6)
@click.pass_context
def bench(ctx, suite, mutants):
    """Run ChipchampBench on the workspace (B1 mutation-detection, B6 triage
    fidelity). Results land in .chipchamp/bench/."""
    import tempfile

    from ..bench import run_b1, run_b6
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    out_dir = c.ws.dot / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="chipchamp-bench-") as work:
        if suite in ("b1", "all"):
            echo("[bold]B1 mutation-detection[/] (injecting real bugs, running the smoke test)")
            r = run_b1(c.ws.root, work, max_mutants=mutants,
                       on_event=lambda m: echo(
                           f"  {'[green]killed[/]' if m['detected'] else '[red]SURVIVED[/]'} "
                           f"{m['mutant']} @ line {m['line']}"))
            reports.append(r)
            echo(f"  detection rate: [bold]{r['detection_rate']}[/] "
                 f"({r['detected']}/{r['total']}, {r['wall_s']}s)"
                 + (f"  [yellow]survivors: {r['survivors']}[/]" if r["survivors"] else ""))
        if suite in ("b6", "all"):
            echo("[bold]B6 triage fidelity[/] (does /triage localize the injected bug?)")
            r = run_b6(c.ws.root, work, max_mutants=min(mutants, 3),
                       on_event=lambda m: echo(
                           f"  {m['mutant']}: detected={m['detected']} localized={m['localized']}"))
            reports.append(r)
            echo(f"  localization rate: [bold]{r['localization_rate']}[/]")
    out = out_dir / "report.json"
    _emit_to = json.dumps(reports, indent=2)
    out.write_text(_emit_to)
    echo(f"[dim]report: {c.rel(str(out))}[/]")


@cli.command()
@click.pass_context
def dashboard(ctx):
    """Generate the read-only static HTML dashboard (SPEC §7.5)."""
    from ..dashboard import generate_dashboard
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    path = generate_dashboard(c.ws)
    echo(f"[green]dashboard:[/] {c.rel(path)}")


@cli.command()
@click.option("--tag", default="smoke")
@click.option("--sarif", default=".chipchamp/ci-results.sarif", help="workspace-relative SARIF report")
@click.pass_context
def ci(ctx, tag, sarif):
    """Headless CI gate (SPEC §7.3): lint + tagged sims; nonzero exit on any
    failure; writes a markdown report for the CI system to attach."""
    from ..tools import all_tools as _t
    from ..tools.paths import ws_path, PathRefused
    from ..jobs.reports import sarif_report
    from ..util.jsonio import atomic_write, dump_json
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    try:
        sarif_path, _ = ws_path(c, sarif, write=True)
        report_path, _ = ws_path(c, ".chipchamp/ci-report.md", write=True)
        if not sarif.endswith(".sarif"):
            raise ValueError("--sarif must name a .sarif file")
    except (PathRefused, ValueError) as e:
        raise click.ClickException(str(e)) from e
    lines = ["# chipchamp ci report", ""]
    ok = True
    lint = _t()["lint.run"].handler(c)
    lint_ok = lint.get("status") == "passed" and lint.get("errors", 1) == 0
    ok &= lint_ok
    lines.append(f"- lint: {'PASS' if lint_ok else 'FAIL'} "
                 f"({lint.get('errors')} errors, {lint.get('warnings')} warnings, "
                 f"job {lint.get('job')})")
    regression = _t()["regress.run"].handler(c, tag=tag, seeds=[1], run_id="ci")
    ok &= not any(regression.get(k) for k in ("error", "denied", "failed", "errored"))
    lines.append("- regression: " + regression.get("summary", regression.get("error", "failed")))
    for kind, path in regression.get("reports", {}).items():
        lines.append(f"- {kind}: {path}")
    # Regression reports include launch/budget errors that have no runner job.
    from ..jobs.record import JobRecord
    from ..util.jsonio import load_json
    report_jobs = [r for r in c.task_jobs if r.kind == "lint"]
    if regression.get("reports", {}).get("json"):
        cases = load_json(ws_path(c, regression["reports"]["json"])[0])["cases"]
        report_jobs.extend(JobRecord.from_dict(case["job"]) for case in cases)
    elif regression.get("error"):
        report_jobs.append(JobRecord("ERR-regression", "sim", "chipchamp", status="error",
                                     summary=regression["error"]))
    if lint.get("error"):
        report_jobs.append(JobRecord("ERR-lint", "lint", "chipchamp", status="error", summary=lint["error"]))
    atomic_write(sarif_path, dump_json(sarif_report(report_jobs, c.ws.root)))
    from ..dashboard import generate_dashboard
    generate_dashboard(c.ws)
    atomic_write(report_path, "\n".join(lines) + "\n")
    if ctx.obj["as_json"]:
        _emit({"ok": ok, "lint": lint, "regression": regression,
               "reports": {"markdown": c.rel(report_path), "sarif": c.rel(sarif_path)}}, True)
    else:
        echo("\n".join(lines))
        echo(f"[dim]report: {c.rel(report_path)} · SARIF: {c.rel(sarif_path)} · dashboard regenerated[/]")
    raise SystemExit(0 if ok else 1)


@cli.command()
@click.option("--tag", default="smoke", help="test tag to regress")
@click.option("--seeds", default=3, help="seeds per test")
@click.option("--triage/--no-triage", default=True,
              help="root-cause the failure clusters with the agent")
@click.pass_context
def nightly(ctx, tag, seeds, triage):
    """The unattended run: regress, accumulate flake history, publish.

    The difference between a tool someone opens and infrastructure a team
    depends on. Everything it needs already existed separately — the parallel
    regression, the signature clustering, the history store, the dashboard —
    but nothing composed them into something cron could call."""
    from ..jobs.regression import RegressionManager
    from ..tools import all_tools as _t
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    tools = _t()
    tests = [t["name"] for t in c.ws.tests_with_tag(tag)]
    if not tests:
        echo(f"[yellow]no tests tagged '{tag}'[/]")
        raise SystemExit(1)
    seed_list = list(range(1, max(1, seeds) + 1))
    echo(f"[bold]{len(tests)}[/] test(s) × {len(seed_list)} seed(s) — tag {tag}")

    def run_one(test: str, seed: int):
        tools["sim.run"].handler(c, test=test, seed=seed)
        return c.task_jobs[-1]

    mgr = RegressionManager(run_one, max_parallel=4)
    res = mgr.run(tests, seeds=seed_list)
    echo(f"\n{res.summary()}")
    # history is what makes tomorrow's run smarter than tonight's: the same
    # seed disagreeing with itself is only visible across runs
    flaky = c.history.flaky_tests()
    if flaky:
        echo(f"\n[bold yellow]{len(flaky)} flaky[/] "
             f"[dim](same seed, different answers — `chipchamp flaky`)[/]")
        for f in flaky[:5]:
            echo(f"  {f['test']} seed {f['seed']} — {f['flake_rate']:.0%} fail "
                 f"over {f['runs']} runs")
    if triage and res.clusters:
        gw, reason, _ = _build_gateway(c)
        if gw is None:
            echo(f"[dim]no model for triage ({reason})[/]")
        else:
            _nightly_triage(c, gw, res)
    from ..dashboard import generate_dashboard
    path = generate_dashboard(c.ws)
    echo(f"\n[dim]dashboard: {c.rel(str(path))}[/]")
    raise SystemExit(0 if not res.clusters else 1)


def _nightly_triage(c, gw, res) -> None:
    """One subagent per failure cluster — the fan-out hardware makes obvious
    and nothing could reach until agent.spawn existed."""
    from ..agent.orchestrator import Orchestrator, SubagentTask
    echo(f"\n[bold]triaging[/] {len(res.clusters)} cluster(s)")
    orch = Orchestrator(c, lambda role: gw, max_concurrent=3,
                        on_event=_agent_events(c))
    tasks = [SubagentTask(role="triage-analyst", label=cl.id,
                          prompt=f"Root-cause this failure cluster. Signature: "
                                 f"{cl.signature}. Representative job: "
                                 f"{getattr(cl, 'representative', '?')}. "
                                 f"Cite file:line and the mechanism.")
             for cl in res.clusters[:6]]
    for r in orch.run(tasks):
        echo(f"\n[bold]{r.label}[/] [dim]{r.role}[/]")
        echo(f"  {r.error or (r.text or '')[:600]}")


@cli.command()
@click.option("--session", "session_id", default="",
              help="session to wake (default: the most recent parked one)")
@click.option("--timeout", default=3600.0, help="seconds to wait per job")
@click.option("--model", default=None)
@click.option("--provider", default=None)
@click.option("--max-steps", default=20)
@click.pass_context
def wake(ctx, session_id, timeout, model, provider, max_steps):
    """Collect a parked session's detached jobs and continue the task.

    This is the other half of `sim.run(detach=true)`. Detaching alone just
    moves the blocking from the runner to whoever is watching the REPL; a job
    that finishes at 3am also needs something to pick the work back up. Run
    this from cron, from a farm epilogue, or by hand in the morning."""
    from ..agent import AgentLoop, NullGateway, Session
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    store = str(c.ws.dot / "sessions")
    sess = (Session.load(session_id, store) if session_id
            else _latest_parked(store))
    if sess is None:
        echo("[dim]no parked session — nothing is waiting on a detached job[/]")
        return
    jobs = sess.task_frame.get("pending_jobs") or []
    if not jobs:
        echo(f"[dim]{sess.id} has no pending jobs[/]")
        return
    echo(f"[bold]{sess.id}[/] parked on {len(jobs)} job(s): {', '.join(jobs)}")
    lines, unfinished = [], []
    for jid in jobs:
        rec = c.runner.wait(jid, timeout=timeout)
        if rec is None or rec.status == "running":
            unfinished.append(jid)
            echo(f"  [yellow]…[/] {jid} still running after {timeout:.0f}s")
            continue
        col = "green" if rec.status == "passed" else "red"
        echo(f"  [{col}]▪[/] {jid} {rec.kind} [{col}]{rec.status}[/] "
             f"[dim]{rec.summary[:70]}[/]")
        lines.append(f"- {jid} ({rec.kind}): {rec.status} — {rec.summary[:200]}")
    # Only the finished ones leave the parking list; an unfinished job stays
    # parked so a later wake still knows to collect it.
    sess.task_frame["pending_jobs"] = unfinished
    sess.save()
    if not lines:
        echo("[yellow]nothing finished[/] — still parked")
        return
    gw, reason, _audit = _build_gateway(c, model, provider)
    if gw is None:
        echo(f"[dim]results recorded; no model to continue with ({reason})[/]")
        return
    task = sess.task_frame.get("parked_task", "")
    events = _agent_events(c)
    loop = AgentLoop(c, gw or NullGateway(), session=sess, max_steps=max_steps,
                     on_event=events, router=_build_router(c),
                     context_budget=_context_budget(c, gw),
                     **c.ws.guards(headless=True).as_kwargs())
    loop.run("The detached jobs you launched have finished:\n"
                   + "\n".join(lines)
                   + f"\n\nContinue the task from here: {task}")
    sess.save()


def _latest_parked(store: str):
    """The most recent session that is waiting on something."""
    from ..agent import Session
    for sid in Session.list_sessions(store):
        s = Session.load(sid, store)
        if s and s.task_frame.get("pending_jobs"):
            return s
    return None


@cli.command()
@click.option("--test", default="", help="one test's history instead of the summary")
@click.pass_context
def flaky(ctx, test):
    """Tests whose own history contradicts itself (SPEC FR-JOB-06).

    A test that fails at seed 3 and passes at seed 4 is not flaky — it found a
    bug at seed 3. Flakiness is disagreement at the SAME seed, which is the
    only place nondeterminism can hide."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    if test:
        seeds = {k.rpartition("#")[2] for k in c.history._load()
                 if k.rpartition("#")[0] == test}
        if not seeds:
            echo(f"[dim]no recorded runs for {test}[/]")
            return
        for s in sorted(seeds):
            v = c.history.verdict(test, None if s == "-" else int(s))
            col = {"stable": "green", "flaky": "yellow",
                   "failing": "red"}.get(v["verdict"], "grey62")
            echo(f"[bold]{test}[/] seed {s}: [{col}]{v['verdict']}[/] "
                 f"[dim]{v.get('passed', 0)}/{v.get('runs', 0)} passed[/]")
            for sig in v.get("signatures", []):
                echo(f"    [dim]{sig[:90]}[/]")
        return
    summ = c.history.summary()
    if ctx.obj.get("as_json"):
        _emit(summ, True)
        return
    if not summ["tracked"]:
        echo("[dim]no test history yet — it accumulates as sims run[/]")
        return
    echo(f"[bold]{summ['tracked']}[/] (test, seed) pair(s) tracked  [dim]"
         + " · ".join(f"{k} {v}" for k, v in sorted(summ["verdicts"].items()))
         + "[/]")
    if not summ["flaky"]:
        echo("[green]no flaky tests[/] [dim]— every seed agrees with itself[/]")
        return
    echo("\n[bold yellow]flaky[/] [dim](same seed, different answers)[/]")
    for f in summ["flaky"]:
        echo(f"  [bold]{f['test']}[/] [dim]seed {f['seed']}[/] — "
             f"[yellow]{f['flake_rate']:.0%}[/] fail over {f['runs']} runs")
        for sig in f.get("signatures", [])[:2]:
            echo(f"    [dim]{sig[:90]}[/]")
