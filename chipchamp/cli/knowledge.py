"""chipchamp CLI — knowledge commands (split from the one-file cli.py)."""
from __future__ import annotations

import os

import click

from .. import brand as _brand

try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _console, _ctx, _emit, _run_tool, cli


@cli.group("notebook", invoke_without_command=True)
@click.pass_context
def notebook_grp(ctx):
    """What this project has learned, across sessions.

    Root causes, fix patterns, hardware constraints — the things not derivable
    from the tree. A digest goes into every system prompt, which is exactly the
    pressure that keeps it short."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(notebook_list_cmd)


@notebook_grp.command("list")
@click.option("--kind", default="", help="root_cause/fix_pattern/constraint/…")
@click.pass_context
def notebook_list_cmd(ctx, kind):
    """Findings, newest first."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    rows = c.notebook.entries(kind)
    if ctx.obj.get("as_json"):
        _emit(rows, True)
        return
    if not rows:
        echo("[dim]nothing learned yet — the agent records findings with "
             "note.add, or add one with `chipchamp notebook add`[/]")
        return
    colour = {"root_cause": "red", "fix_pattern": "green", "constraint": "yellow",
              "decision": "cyan", "gotcha": "magenta"}
    for e in rows:
        echo(f"[{colour.get(e['kind'], 'white')}]{e['kind']:12}[/] "
             f"[bold]{e['subject']}[/] [dim]{e['id']}[/]")
        echo(f"  {e['detail'][:300]}")
        if e.get("evidence"):
            echo(f"  [dim]evidence: {e['evidence']}[/]")


@notebook_grp.command("add")
@click.argument("kind")
@click.argument("subject")
@click.argument("detail")
@click.option("--evidence", default="")
@click.pass_context
def notebook_add_cmd(ctx, kind, subject, detail, evidence):
    """Record a finding by hand (kind: root_cause/fix_pattern/constraint/…)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    out = c.notebook.add(kind, subject, detail, evidence=evidence, source="human")
    if out.get("error"):
        echo(f"[red]{out['error']}[/]")
        raise SystemExit(1)
    echo(f"[green]recorded[/] {out['id']}")


@notebook_grp.command("rm")
@click.argument("note_id")
@click.pass_context
def notebook_rm_cmd(ctx, note_id):
    """Delete a finding — a wrong one is worse than none."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    if c.notebook.remove(note_id):
        echo(f"[green]removed[/] {note_id}")
    else:
        echo(f"[yellow]no note {note_id}[/]")
        raise SystemExit(1)


@cli.group("experiment", invoke_without_command=True)
@click.pass_context
def experiment_grp(ctx):
    """Recorded agent runs (SPEC §17.2) — the substrate for an autopsy.

    Record one with `chipchamp --experiment <label> -p "<task>"`, then compare
    them here. Nothing is recorded unless asked for: an unlabelled run leaves
    no file, so the store stays a deliberate record rather than a log."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(experiment_list_cmd)


@experiment_grp.command("list")
@click.option("--limit", default=25)
@click.pass_context
def experiment_list_cmd(ctx, limit):
    """Recorded runs, newest first."""
    from ..bench.experiment import load_experiments
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    exps = load_experiments(c.ws.dot, limit=limit)
    if not exps:
        echo("[dim]no experiments recorded — "
             "`chipchamp --experiment <label> -p \"<task>\"`[/]")
        return
    if ctx.obj.get("as_json"):
        _emit([e.to_dict() for e in exps], True)
        return
    for e in exps:
        m = e.metrics or {}
        colour = {"completed": "green", "answered": "cyan"}.get(e.outcome, "yellow")
        echo(f"[bold]{e.id}[/]  [{colour}]{e.outcome:9}[/] "
             f"[dim]{e.label or '-'}[/]")
        echo(f"  [dim]{e.model or '?'} · {m.get('steps', 0)} steps · "
             f"{m.get('wall_s', 0):.0f}s · {m.get('tokens_total', 0)} tok · "
             f"{m.get('jobs', 0)} jobs · ctx peak "
             f"{m.get('context_chars_max', 0) // 1000}k chars[/]")


@experiment_grp.command("show")
@click.argument("exp_id")
@click.pass_context
def experiment_show_cmd(ctx, exp_id):
    """Everything recorded about one run."""
    from ..bench.experiment import load_experiments
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    hit = next((e for e in load_experiments(c.ws.dot)
                if e.id == exp_id or e.id.endswith(exp_id)), None)
    if hit is None:
        echo(f"[yellow]no experiment '{exp_id}'[/]")
        raise SystemExit(1)
    _emit(hit.to_dict(), True)


@experiment_grp.command("autopsy")
@click.option("--label", default="", help="only runs with this label")
@click.pass_context
def experiment_autopsy_cmd(ctx, label):
    """Compare recorded runs: per-model outcomes, cost, failing tools."""
    from ..bench.experiment import autopsy, load_experiments
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    exps = [e for e in load_experiments(c.ws.dot)
            if not label or e.label == label]
    if not exps:
        echo("[dim]nothing to autopsy[/]")
        return
    rep = autopsy(exps)
    if ctx.obj.get("as_json"):
        _emit(rep, True)
        return
    echo(f"[bold]{rep['runs']}[/] run(s)"
         + (f" labelled [bold]{label}[/]" if label else ""))
    for model, m in sorted(rep["by_model"].items(),
                           key=lambda kv: -kv[1]["completion_rate"]):
        outs = " ".join(f"{k}={v}" for k, v in sorted(m["outcomes"].items()))
        echo(f"\n[bold cyan]{model}[/] [dim]n={m['runs']}[/]")
        echo(f"  completed {m['completion_rate']:.0%}  [dim]{outs}[/]")
        echo(f"  [dim]avg {m['avg_steps']} steps · {m['avg_wall_s']:.0f}s · "
             f"{m['avg_tokens']} tok · jobs {m['jobs_passed']}/{m['jobs']} passed"
             f" · {m['switches']} switch(es) · ctx peak "
             f"{m['context_chars_max'] // 1000}k chars[/]")
    if rep["tool_failures"]:
        echo("\n[bold]tools that failed most[/]")
        for name, n in rep["tool_failures"].items():
            echo(f"  [yellow]{n:3}[/] {name}")


@cli.command()
@click.pass_context
def packs(ctx):
    """List available knowledge packs (protocol/methodology collateral)."""
    from ..packs import list_packs
    for p in list_packs(ctx.obj["root"]):
        echo(f"[bold]{p['name']}[/] — {p['summary']}")
        echo(f"  [dim]{p['path']} · {len(p['files'])} file(s)[/]")


@cli.group("skills", invoke_without_command=True)
@click.pass_context
def skills_grp(ctx):
    """Manage skill directories (expert playbooks, SKILL.md format)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(skills_list_cmd)


def _skills_ws(ctx):
    return _ctx(ctx.obj["root"], ctx.obj["target"]).ws


@skills_grp.command("add")
@click.argument("directory")
@click.pass_context
def skills_add_cmd(ctx, directory):
    """Point a directory containing skills (each: <name>/SKILL.md)."""
    import os as _os

    from ..skills import add_registry_dir, discover_report
    ws = _skills_ws(ctx)
    d = _os.path.abspath(_os.path.expanduser(directory))
    if not _os.path.isdir(d):
        echo(f"[red]not a directory: {directory}[/]")
        raise SystemExit(1)
    add_registry_dir(ws, d)
    rep = discover_report(ws)
    mine = [s for s in rep.skills.values() if str(s.dir).startswith(d)]
    shadowed = [w for w in rep.shadowed if f"({d}" in w]
    echo(f"[green]registered[/] {d} — {len(mine)} skill(s) found"
         + (f" [yellow]({len(shadowed)} shadowed by earlier dirs)[/]"
            if shadowed else ""))
    for w in shadowed[:5]:
        echo(f"  [yellow]{w}[/]")


@skills_grp.command("list")
@click.pass_context
def skills_list_cmd(ctx):
    """Show pointed directories and every discovered skill."""
    from ..skills import discover_report, first_sentence, skill_dirs
    ws = _skills_ws(ctx)
    rep = discover_report(ws)
    if ctx.obj["as_json"]:
        _emit({"dirs": [{"path": str(p), "source": src}
                        for p, src in skill_dirs(ws)],
               "skills": [{"name": s.name, "description": s.description,
                           "dir": str(s.dir), "source": s.source}
                          for s in rep.skills.values()],
               "shadowed": rep.shadowed, "warnings": rep.warnings}, True)
        return
    dirs = skill_dirs(ws)
    if not dirs:
        echo("[dim]no skill directories — add one with[/] "
             "[bold]chipchamp skills add <dir>[/]")
        return
    echo("[bold]directories[/]")
    for p, src in dirs:
        echo(f"  {p}  [dim]({src})[/]")
    echo(f"[bold]skills[/] ({len(rep.skills)})")
    for s in rep.skills.values():
        echo(f"  [bold]{s.name:24}[/] {first_sentence(s.description)[:72]}")
    for w in rep.shadowed:
        echo(f"[yellow]shadowed: {w}[/]")
    for w in rep.warnings:
        echo(f"[yellow]warning: {w}[/]")


@skills_grp.command("remove")
@click.argument("directory", required=False)
@click.option("--all", "remove_all", is_flag=True, help="remove every pointer")
@click.pass_context
def skills_remove_cmd(ctx, directory, remove_all):
    """Remove a pointed directory (or all of them)."""
    from ..skills import load_registry, remove_registry_dir
    ws = _skills_ws(ctx)
    if remove_all:
        remove_registry_dir(ws, all=True)
        echo("[green]cleared all registered skill directories[/]")
        return
    if not directory:
        echo("[red]give a directory, or --all[/]")
        raise SystemExit(1)
    import os as _os
    d = _os.path.abspath(_os.path.expanduser(directory))
    before = load_registry(ws)
    after = remove_registry_dir(ws, d)
    if len(after) < len(before):
        echo(f"[green]removed[/] {d}")
    else:
        echo(f"[yellow]{d} was not registered[/] — `chipchamp skills list` "
             f"shows sources (workspace/config dirs aren't registry-managed)")


@skills_grp.command("reindex")
@click.pass_context
def skills_reindex_cmd(ctx):
    """(Re)build the semantic-trigger embedding cache for every skill."""
    from ..skills import discover
    from ..skills_semantic import cache_path, corpus_vectors, resolve_endpoint
    ws = _skills_ws(ctx)
    sk = discover(ws)
    if not sk:
        echo("[dim]no skills to index — add a directory first[/]")
        return
    try:
        base, model = resolve_endpoint(ws)
        vecs = corpus_vectors(ws, sk)
    except Exception as e:
        echo(f"[red]semantic index unavailable:[/] {e}")
        echo("[dim]the agent falls back to the static compact index; set "
             "[skills] embed_provider/embed_model in config.toml to pin an "
             "endpoint[/]")
        raise SystemExit(1)
    echo(f"[green]indexed[/] {len(vecs)} skill(s) with [bold]{model}[/] at "
         f"{base} → {ws.rel(str(cache_path(ws)))}")


@skills_grp.command("show")
@click.argument("name")
@click.pass_context
def skills_show_cmd(ctx, name):
    """Print one skill's playbook."""
    from ..skills import discover, skill_body
    ws = _skills_ws(ctx)
    sk = discover(ws)
    s = sk.get(name)
    if s is None:
        import difflib
        close = difflib.get_close_matches(name, list(sk), n=3, cutoff=0.5)
        echo(f"[red]unknown skill '{name}'[/]"
             + (f" — did you mean: {', '.join(close)}?" if close else ""))
        raise SystemExit(1)
    body = skill_body(s)
    if ctx.obj["as_json"]:
        _emit(body, True)
        return
    echo(f"[dim]{s.dir}  ·  source: {s.source}"
         + (f"  ·  files: {', '.join(body['files'])}" if body["files"] else "")
         + "[/]")
    if _console():
        from .. import ui
        ui.markdown(body["body"])
    else:
        echo(body["body"])


@cli.group("library", invoke_without_command=True)
@click.pass_context
def library_grp(ctx):
    """Manage IP library directories (manifest.json component libraries)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(library_list_cmd)


@library_grp.command("add")
@click.argument("directory")
@click.pass_context
def library_add_cmd(ctx, directory):
    """Point a library root (a directory carrying manifest.json)."""
    import os as _os

    from ..library import add_registry_dir, discover_report
    ws = _skills_ws(ctx)
    d = _os.path.abspath(_os.path.expanduser(directory))
    if not _os.path.isdir(d):
        echo(f"[red]not a directory: {directory}[/]")
        raise SystemExit(1)
    if not _os.path.isfile(_os.path.join(d, "manifest.json")):
        echo(f"[red]{directory} has no manifest.json — not a component library[/]")
        raise SystemExit(1)
    add_registry_dir(ws, d)
    rep = discover_report(ws)
    mine = [m for m in rep.modules.values() if str(m.root) == d]
    echo(f"[green]registered[/] {d} — {len(mine)} component(s) found")
    for w in rep.warnings[:5]:
        echo(f"  [yellow]{w}[/]")


@library_grp.command("list")
@click.option("--category", default="", help="filter by category prefix")
@click.pass_context
def library_list_cmd(ctx, category=""):
    """Show registered libraries and their components."""
    from ..library import discover_report, library_dirs
    ws = _skills_ws(ctx)
    rep = discover_report(ws)
    if ctx.obj["as_json"]:
        _emit({"dirs": [{"path": str(p), "source": src}
                        for p, src in library_dirs(ws)],
               "libraries": rep.libraries,
               "components": [{"name": m.name, "category": m.category,
                               "summary": m.summary, "dir": str(m.dir),
                               "status": m.meta.get("status", "")}
                              for m in rep.modules.values()
                              if not category
                              or m.category.startswith(category)],
               "shadowed": rep.shadowed, "warnings": rep.warnings}, True)
        return
    if not rep.libraries:
        echo("[dim]no IP libraries — add one with[/] "
             "[bold]chipchamp library add <dir>[/]")
        return
    for lib in rep.libraries:
        echo(f"[bold]{lib['name']}[/]  {lib['root']}  "
             f"[dim]{lib['count']} component(s) ({lib['source']})[/]")
    cur = None
    for m in sorted(rep.modules.values(), key=lambda x: (x.category, x.name)):
        if category and not m.category.startswith(category):
            continue
        if m.category != cur:
            cur = m.category
            echo(f"  [bold]{cur}[/]")
        echo(f"    {m.name:24} {m.summary[:66]}")
    for w in rep.warnings:
        echo(f"[yellow]warning: {w}[/]")


@library_grp.command("remove")
@click.argument("directory", required=False)
@click.option("--all", "remove_all", is_flag=True, help="remove every pointer")
@click.pass_context
def library_remove_cmd(ctx, directory, remove_all):
    """Remove a pointed library (or all of them)."""
    from ..library import load_registry, remove_registry_dir
    ws = _skills_ws(ctx)
    if remove_all:
        remove_registry_dir(ws, all=True)
        echo("[green]cleared all registered library directories[/]")
        return
    if not directory:
        echo("[red]give a directory, or --all[/]")
        raise SystemExit(1)
    import os as _os
    d = _os.path.abspath(_os.path.expanduser(directory))
    before = load_registry(ws)
    after = remove_registry_dir(ws, d)
    if len(after) < len(before):
        echo(f"[green]removed[/] {d}")
    else:
        echo(f"[yellow]{d} was not registered[/] — config-declared dirs "
             f"aren't registry-managed")


@library_grp.command("show")
@click.argument("name")
@click.pass_context
def library_show_cmd(ctx, name):
    """Print one component's card (params/ports + instantiation template)."""
    from ..library import discover, module_card
    ws = _skills_ws(ctx)
    mods = discover(ws)
    m = mods.get(name)
    if m is None:
        import difflib
        close = difflib.get_close_matches(name, list(mods), n=3, cutoff=0.5)
        echo(f"[red]unknown component '{name}'[/]"
             + (f" — did you mean: {', '.join(close)}?" if close else ""))
        raise SystemExit(1)
    card = module_card(m)
    if ctx.obj["as_json"]:
        _emit(card, True)
        return
    echo(f"[bold]{card['name']}[/]  [{card['category']}]  "
         f"v{card['version']}  [dim]{card['status']} · {card['dir']}[/]")
    echo(card["summary"])
    if card["params"]:
        echo("[bold]params[/]")
        for p in card["params"]:
            echo(f"  {p.get('name', ''):16} default={p.get('default', '')} "
                 f"[dim]{p.get('desc', '')}[/]")
    echo("[bold]instantiation[/]")
    echo(card["instantiation"])


@library_grp.command("search")
@click.argument("query")
@click.option("--category", default="")
@click.pass_context
def library_search_cmd(ctx, query, category):
    """Keyword search over the component manifests."""
    from ..library import search
    ws = _skills_ws(ctx)
    hits = search(ws, query=query, category=category)
    if ctx.obj["as_json"]:
        _emit([{"name": m.name, "category": m.category, "summary": m.summary}
               for m in hits], True)
        return
    if not hits:
        echo("[dim]no match — `chipchamp library list` shows the category map[/]")
        return
    for m in hits:
        echo(f"  [bold]{m.name:24}[/] [{m.category}] {m.summary[:56]}")


@library_grp.command("match")
@click.argument("behavior")
@click.option("--interface", default="", help="protocol/ports/handshake/clocks hint")
@click.option("--category", default="")
@click.pass_context
def library_match_cmd(ctx, behavior, interface, category):
    """Match components by function + interface (name-independent)."""
    from ..library_semantic import match
    ws = _skills_ws(ctx)
    try:
        hits = match(ws, behavior, interface=interface, category=category)
    except Exception as e:
        echo(f"[red]match unavailable:[/] {e}")
        raise SystemExit(1)
    if ctx.obj["as_json"]:
        _emit([{"name": m.name, "category": m.category, "score": round(sc, 3),
                "semantic": info["semantic"], "interface": info["interface_signature"],
                "why": info["why"]} for m, sc, info in hits], True)
        return
    if not hits:
        echo("[dim]no components — is a library registered?[/]")
        return
    for m, sc, info in hits:
        why = ", ".join(info["why"]) or "functional similarity"
        echo(f"  [bold]{m.name:22}[/] [dim]{sc:.2f}[/] [{m.category}] "
             f"[dim]{m.summary[:50]}[/]")
        echo(f"      [dim]{info['interface_signature'][:100]}[/]")
        echo(f"      [cyan]why:[/] {why}")


@library_grp.command("reindex")
@click.pass_context
def library_reindex_cmd(ctx):
    """(Re)build the semantic-match embedding cache for every component."""
    from ..library import discover
    from ..library_semantic import cache_path, component_vectors
    from ..embeddings import resolve_endpoint
    ws = _skills_ws(ctx)
    mods = discover(ws)
    if not mods:
        echo("[dim]no components — register a library with `library add <dir>`[/]")
        return
    try:
        cfg = ws.library_cfg
        base, model = resolve_endpoint(ws, str(cfg.get("embed_provider", "")).strip(),
                                       str(cfg.get("embed_model", "")).strip())
        vecs = component_vectors(ws, mods)
    except Exception as e:
        echo(f"[red]semantic match unavailable:[/] {e}")
        echo("[dim]lib.match falls back to lexical + interface scoring; set "
             "[library] embed_provider/embed_model to pin an endpoint[/]")
        raise SystemExit(1)
    echo(f"[green]indexed[/] {len(vecs)} component(s) with [bold]{model}[/] at "
         f"{base} → {ws.rel(str(cache_path(ws)))}")


@library_grp.command("fetch")
@click.argument("name")
@click.option("--view", type=click.Choice(["rtl", "sim"]), default="rtl",
              help="rtl = synthesizable fileset; sim = C++ golden ref model")
@click.option("--dest", default="", help="workspace-relative destination")
@click.pass_context
def library_fetch_cmd(ctx, name, view, dest):
    """Copy a component into the workspace (rtl fileset or C++ ref model)."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    res = _run_tool(c, "lib.fetch", name=name, view=view, dest=dest)
    if ctx.obj["as_json"]:
        _emit(res, True)
        return
    if "error" in res:
        echo(f"[red]{res['error']}[/]")
        raise SystemExit(1)
    for f in res["written"]:
        echo(f"  [green]fetched[/] {f}")
    for f in res["unchanged"]:
        echo(f"  [dim]unchanged {f}[/]")
    for d in res.get("denied", []):
        echo(f"  [red]denied {d['path']}: {d['reason']}[/]")
    echo(f"[dim]{res['note']}[/]")
    if "instantiation" not in res:
        return
    echo("[bold]instantiation[/]")
    echo(res["instantiation"])


@cli.command()
@click.argument("path", required=False)
@click.option("--unpin", is_flag=True, help="clear the pinned default working repo")
@click.pass_context
def workspace(ctx, path, unpin):
    """Show the working repo, or pin/clear a default one.

    `chipchamp workspace`            → show the resolved repo and how it was chosen
    `chipchamp workspace <path>`     → pin <path> as the default (used from anywhere)
    `chipchamp workspace .`          → pin the current directory
    `chipchamp workspace --unpin`    → clear the pinned default
    """
    from ..config import find_project_root, pin_root, pinned_root, unpin_root
    if unpin:
        echo("[green]unpinned[/]" if unpin_root() else "[dim]no pinned default[/]")
        return
    if path is not None:
        pinned = pin_root(path)
        echo(f"[green]pinned default working repo:[/] {pinned}")
        return
    root = ctx.obj["root"]
    how = ctx.obj.get("root_how", "")
    echo(f"[bold]working repo:[/] {root}  [dim]({how})[/]")
    echo(f"[dim]cwd:[/] {os.getcwd()}")
    echo(f"[dim]git/project root of cwd:[/] {find_project_root(os.getcwd())}")
    pin = pinned_root()
    echo(f"[dim]pinned default:[/] {pin or '(none)'}")
    echo(f"[dim]precedence: --root > {_brand.env_name('ROOT')} > pinned > auto-detect[/]")


@cli.command("mcp-serve")
@click.pass_context
def mcp_serve(ctx):
    """Serve the design DB / jobs / waves as a read-only MCP server on stdio."""
    from ..mcp import McpServer
    McpServer(ctx.obj["root"], ctx.obj["target"]).serve()
