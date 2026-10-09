"""Explainable verification planning; selection never relaxes policy gates."""
from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict, deque
from pathlib import Path

from .design import DesignDB
from .policy.diff import Diff, FileChange
from .util.hashing import sha256_text


def target_tests(ctx) -> list[dict]:
    return [t for t in ctx.ws.tests if t.get("name")
            and t.get("target", ctx.target_name) == ctx.target_name]


def regression_seeds(ctx, seeds=None) -> list[int]:
    values = seeds if seeds is not None else ctx.ws.test_defaults.get("seeds", [1])
    if not isinstance(values, (list, tuple)) or not values or any(
            not isinstance(s, int) or isinstance(s, bool) for s in values):
        raise ValueError("seeds must be a non-empty list of integers")
    return list(dict.fromkeys(values))


def _changes(ctx, paths, base):
    from .tools.paths import ws_path
    if paths is None and ctx.edits:
        diff = ctx.current_diff()
        for f in diff.files:
            ws_path(ctx, f.path)
        return diff, "session", []

    def git(*args):
        return subprocess.run(["git", "-C", ctx.ws.root, *args],
                              capture_output=True, text=True, timeout=30)

    warnings = []
    head = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    revision = head.stdout.strip() if head.returncode == 0 else ""
    if not revision and base != "HEAD":
        raise ValueError(f"cannot resolve base revision: {base}")
    prefix = git("rev-parse", "--show-prefix").stdout.rstrip("\n") if revision else ""
    source = "explicit" if paths is not None else "git"
    if paths is None:
        changed = git("diff", "--name-only", "--no-renames", "--relative", "-z",
                      revision, "--", ".") if revision else None
        untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--", ".")
        paths = ((changed.stdout.split("\0") if changed and changed.returncode == 0 else [])
                 + (untracked.stdout.split("\0") if untracked.returncode == 0 else []))
        if not revision:
            warnings.append("No committed Git baseline; pass changed_paths to scope the plan.")
    files = []
    for path in sorted(set(p for p in paths if p)):
        ap, rel = ws_path(ctx, path)
        if os.path.isdir(ap):
            raise ValueError(f"changed_paths must name files, not directories: {rel}")
        old = git("show", f"{revision}:{prefix}{rel}") if revision else None
        exists = os.path.isfile(ap)
        if not exists and not (old and old.returncode == 0):
            raise ValueError(f"changed file does not exist and is absent from the base: {rel}")
        files.append(FileChange(
            rel, "deleted" if not exists else "modified" if old and old.returncode == 0 else "added",
            old.stdout if old and old.returncode == 0 else "",
            Path(ap).read_text(errors="replace") if exists else ""))
    return Diff(files), source, warnings


def _runner(test):
    return test.get("runner") or test


def _selection(ctx, diff, tests):
    from .tools.paths import PathRefused, ws_path
    # A fresh in-memory index includes benches omitted from the target filelist.
    db = DesignDB(root=ctx.ws.root)
    inputs = set(ctx.target.sources)
    incdirs = set(ctx.target.incdirs)
    for test in tests:
        runner = _runner(test)
        for key in ("files", "rtl"):
            inputs.update(os.path.join(ctx.ws.root, p) for p in runner.get(key, []))
        incdirs.update(os.path.join(ctx.ws.root, p) for p in runner.get("incdirs", []))
    warnings = []
    for path in sorted(inputs):
        try:
            ap, _ = ws_path(ctx, ctx.rel(path))
        except PathRefused:
            warnings.append("Some design inputs are outside the readable workspace.")
            continue
        if Path(ap).suffix.lower() in (".sv", ".v", ".svh", ".vh", ".vhd", ".vhdl"):
            db.add_file(ap, defines=ctx.target.defines, incdirs=sorted(incdirs))
    db.build()
    if db.warnings or any(m.parse_confidence != "high" for m in db.modules.values()):
        warnings.append("The design index is incomplete or degraded.")

    by_file, parents = defaultdict(set), defaultdict(set)
    for name, mod in db.modules.items():
        by_file[os.path.realpath(mod.file)].add(name)
        for instance in mod.instances:
            parents[instance.module_type].add(name)
    direct = set()
    for change in diff.files:
        names = by_file.get(os.path.realpath(os.path.join(ctx.ws.root, change.path)), set())
        if (change.status == "deleted" or not names
                or Path(change.path).suffix.lower() not in (".sv", ".v")) and not change.is_doc:
            warnings.append(f"{change.path}: impact cannot be isolated to indexed modules.")
        direct.update(names)
    affected = set(direct)
    queue = deque(sorted(direct))
    while queue:
        for parent in parents[queue.popleft()] - affected:
            affected.add(parent)
            queue.append(parent)

    def unresolved(top):
        seen, pending = set(), [top]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            mod = db.modules.get(name)
            if mod is None:
                return True
            pending.extend(i.module_type for i in mod.instances)
        return False

    reasons = {}
    broad = bool(warnings) or not direct
    for test in tests:
        runner = _runner(test)
        top = (runner.get("tb_top") or runner.get("toplevel") or runner.get("top")
               or "tb_" + test["name"])
        if broad:
            reasons[test["name"]] = ["Full suite: impact is unknown or no module change was identified."]
        elif top in affected:
            reasons[test["name"]] = [f"{top} contains a changed module or is changed itself."]
        elif unresolved(top):
            reasons[test["name"]] = [f"{top}: unresolved hierarchy; retained conservatively."]
    if tests and not reasons:
        warnings.append("No test maps to the changed modules; the full suite is required for feedback.")
        reasons = {t["name"]: ["Full suite: no affected test could be established."] for t in tests}
    selected = sorted(reasons)
    return {"mode": "full_suite" if len(selected) == len(tests) else "targeted",
            "selected_tests": selected, "reasons": reasons,
            "excluded_tests": sorted(t["name"] for t in tests if t["name"] not in reasons),
            "changed_modules": sorted(direct), "affected_modules": sorted(affected),
            "warnings": list(dict.fromkeys(warnings)), "frontend": db.frontend,
            "design_hash": db.tree_rev}


def verification_plan(ctx, changed_paths=None, seeds=None, base="HEAD") -> dict:
    ctx.ws.invalidate(ctx.target_name)
    if changed_paths is not None and (not isinstance(changed_paths, (list, tuple))
            or any(not isinstance(p, str) or not p for p in changed_paths)):
        raise ValueError("changed_paths must be a list of non-empty workspace-relative file paths")
    seeds = regression_seeds(ctx, seeds)
    diff, source, warnings = _changes(ctx, changed_paths, base)
    tests = target_tests(ctx)
    names = [t["name"] for t in tests]
    if len(set(names)) != len(names):
        raise ValueError("test names must be unique within the active target")
    selection = _selection(ctx, diff, tests)
    selection["warnings"] = warnings + selection["warnings"]
    policy = ctx.policy.check(diff)
    jobs = len(selection["selected_tests"]) * len(seeds)
    remaining = max(0, ctx.ledger.budget.v3_submissions - ctx.ledger.v3_used)
    out = {"schema_version": 1, "target": ctx.target_name, "change_source": source,
           "changed_files": [f.path for f in diff.files],
           "verification_hash": ctx.verification_hash(),
           "task_classes": policy.task_classes, "required_gates": policy.required_gates,
           "minimum_rung": policy.min_rung, "selection": selection, "seeds": seeds,
           "test_jobs": jobs, "declared_suite_jobs": len(tests) * len(seeds),
           "budget": {"v3_remaining": remaining, "v3_required": jobs,
                      "within_budget": jobs <= remaining},
           "note": "Targeted runs provide feedback. All policy gates and declared-suite obligations still apply to report.done."}
    if not tests:
        selection["warnings"].append("No tests are declared for the active target.")
        selection["mode"] = "no_tests"
    out["plan_id"] = "VP-" + sha256_text(json.dumps(out, sort_keys=True))[:16]
    return out
