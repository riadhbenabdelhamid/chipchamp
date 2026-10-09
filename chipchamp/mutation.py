"""Changed-line mutation experiments with baseline checks and bounded execution."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import difflib
import math
from pathlib import Path
import re
import shutil
import time
import uuid

from .bench.mutators import generate_mutants
from .bench.runner import MutationRunner
from .bench.workspace import snapshot, validate_inputs
from .config import Workspace
from .coverage.history import CoverageHistory
from .coverage.impact import fallback_reason, regions
from .tools.context import ToolContext
from .tools.paths import PathRefused, ws_path
from .util.hashing import sha256_text
from .util.jsonio import atomic_write, dump_json
from .verification import _changes, regression_seeds, target_tests


NOTE = ("Mutation results apply only to the recorded tests, seeds and inputs. "
        "Survivors need review: a mutation may preserve behavior. Errors and "
        "timeouts never count as detection. Experiments do not satisfy completion gates.")


def _limit(name, value, maximum, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value <= 0 or value > maximum or
            (integer and not isinstance(value, int))):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'} <= {maximum}")


def _candidates(ctx, diff, maximum):
    candidates, skipped = [], []
    sources = {str(Path(p).resolve()) for p in ctx.target.sources}
    truncated = False
    for change in diff.files:
        path, relative = ws_path(ctx, change.path)
        reason = fallback_reason(relative, change.new_text)
        allowed, why = ctx.policy.can_write(relative)
        if not allowed:
            reason = why
        elif str(Path(path).resolve()) not in sources:
            reason = "not a source of the active RTL target"
        spans = [r["new_lines"] for r in regions(change) if r["new_lines"]]
        if not spans:
            reason = "no added or modified current lines (deletions cannot be mutated)"
        if reason:
            skipped.append({"path": relative, "reason": reason})
            continue
        lines = {n for start, end in spans for n in range(start, end + 1)}
        if Path(path).read_text() != change.new_text:
            raise ValueError("changed RTL moved during mutation planning; retry")
        current = Path(path).read_bytes().decode("utf-8")
        found = generate_mutants(current, relative, per_op=maximum + 1,
                                 lines=lines, limit=maximum + 1)
        if not found:
            skipped.append({"path": relative, "reason": "no supported mutation operator on changed lines"})
        for mutant in found:
            if len(candidates) == maximum:
                truncated = True
                break
            mutant.id = "MU-" + sha256_text(relative + "\0" + mutant.mutated)[:16]
            candidates.append(mutant)
    return candidates, skipped, truncated


def _assertion_failure(rec):
    """Require a check failure, not just an arbitrary nonzero simulator exit."""
    from .brand import markers
    metrics = rec.result.get("metrics", {})
    if any((metrics.get(key) or 0) > 0 for key in
           ("assertion_failures", "failed", "uvm_errors", "uvm_fatals")):
        return True
    if not rec.steps:
        return False
    with open(rec.steps[-1].log_path, errors="replace") as stream:
        log = stream.read(1_000_000)
    return (any(marker in log for marker in markers("FAIL")) or
            bool(re.search(r"(?im)(?:\$fatal\b|^FATAL:\s+.*:\d+|assert(?:ion)?[^\n]*fail|UVM_(?:ERROR|FATAL)\s+[^:\s])", log)))


def classify(rec, plan):
    if rec.status == "timeout" or any(s.timed_out for s in rec.steps):
        return "timeout"
    # Native runners build before their final simulation step. Cocotb combines
    # those phases; without its results XML an error remains inconclusive.
    if (rec.result.get("metrics", {}).get("phase") == "compile" or
            any(s.rc != 0 for s in rec.steps[:len(plan.steps) - 1])):
        return "compile_error" if all(s.rc not in (126, 127) for s in rec.steps) else "error"
    if len(rec.steps) != len(plan.steps) or not rec.steps:
        return "error"
    if rec.result.get("status") == "fail" and _assertion_failure(rec):
        return "detected"
    if (rec.status == "passed" and rec.result.get("status") == "pass"
            and all(s.rc == 0 for s in rec.steps)):
        return "passed"
    return "error"


def run_mutations(ctx, changed_paths=None, tests=None, seeds=None, base="HEAD",
                  max_mutants=8, max_jobs=20, timeout_s=120, max_seconds=600):
    """Run every baseline case; stop each mutant after its first detected check failure."""
    from .tools.verif_tools import _plan_for_test, _runner_of
    from .bench.mutation_report import markdown

    start = time.monotonic()
    _limit("max_mutants", max_mutants, 100, True)
    _limit("max_jobs", max_jobs, 1000, True)
    _limit("timeout_s", timeout_s, 14400)
    _limit("max_seconds", max_seconds, 14400)
    if ctx.runner.farm.kind != "local":
        raise ValueError("mutation runtime limits currently require the local runner")
    if changed_paths is not None and (not isinstance(changed_paths, (list, tuple)) or
            any(not isinstance(p, str) or not p for p in changed_paths)):
        raise ValueError("changed_paths must be a list of non-empty workspace-relative file paths")
    ctx.ws.invalidate(ctx.target_name)
    declared = target_tests(ctx)
    names = [t["name"] for t in declared]
    if len(set(names)) != len(names):
        raise ValueError("test names must be unique within the active target")
    if tests is not None:
        if not isinstance(tests, (list, tuple)) or not tests or any(t not in names for t in tests):
            raise ValueError("tests must name a non-empty selection from the active target")
        names = list(dict.fromkeys(tests))
    if not names:
        raise ValueError("no tests declared for the active target")
    seeds = regression_seeds(ctx, seeds)
    diff, source, warnings = _changes(ctx, changed_paths, base)
    mutants, skipped, truncated = _candidates(ctx, diff, max_mutants)
    ordering = CoverageHistory(ctx).order(names, diff)
    names = ordering["tests"]
    matrix = [{"test": test, "seed": seed} for test in names for seed in seeds]
    campaign = "M-" + uuid.uuid4().hex[:16]
    path, relative = ws_path(ctx, f"{ctx.ws.dot.name}/mutations/{campaign}", write=True)
    root = Path(path)
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "id": campaign, "target": ctx.target_name,
              "status": "running", "change_source": source, "base": base,
              "ordering": ordering, "matrix": matrix, "baseline": [],
              "mutants": [], "skipped": skipped, "candidate_limit_reached": truncated,
              "warnings": warnings, "note": NOTE,
              "limits": {"max_mutants": max_mutants, "max_jobs": max_jobs,
                         "timeout_s": timeout_s, "max_seconds": max_seconds,
                         "max_copy_bytes": 1024**3},
              "jobs_submitted": 0, "maximum_jobs_needed": len(matrix) * (1 + len(mutants)),
              "reports": {"json": f"{relative}/report.json", "markdown": f"{relative}/report.md"}}
    for mutant in mutants:
        patch = "".join(difflib.unified_diff(mutant.original.splitlines(True),
                         mutant.mutated.splitlines(True), fromfile="a/" + mutant.file,
                         tofile="b/" + mutant.file))
        patch_path = root / "patches" / (mutant.id + ".patch")
        atomic_write(patch_path, patch)
        report["mutants"].append({"id": mutant.id, "path": mutant.file, "line": mutant.line,
            "description": mutant.description, "source_sha256": sha256_text(mutant.original),
            "mutated_sha256": sha256_text(mutant.mutated), "patch": ctx.rel(patch_path),
            "status": "not_run", "cases": []})

    def save():
        counts = Counter(m["status"] for m in report["mutants"])
        report["counts"] = dict(counts)
        meaningful = counts["detected"] + counts["survived"]
        report["detection_rate"] = round(counts["detected"] / meaningful, 4) if meaningful else None
        report["elapsed_s"] = round(time.monotonic() - start, 3)
        report["budget"] = ctx.ledger.snapshot()
        atomic_write(root / "report.json", dump_json(report))
        atomic_write(root / "report.md", markdown(report))

    save()
    if not mutants:
        report["status"] = "no_mutations"
        save()
        return report
    deadline = start + max_seconds
    runner = MutationRunner(root / "jobs", ctx.runner)

    def stop_reason():
        if time.monotonic() >= deadline:
            return "runtime limit reached"
        if report["jobs_submitted"] >= max_jobs:
            return "job limit reached"
        ok, why = ctx.ledger.can_submit_v3()
        if not ok:
            return why
        if ctx.ledger.cpu_seconds >= ctx.ledger.budget.cpu_hours * 3600:
            return "CPU-hour budget exhausted"
        return ""

    def run_case(case, variant, mutant=None):
        why = stop_reason()
        if why:
            raise LimitReached(why)
        if report.get("copied_bytes", 0) + report["inputs"]["bytes"] > 1024**3:
            raise LimitReached("mutation workspace copies reached the 1 GiB limit")
        # Fresh inputs even across seeds: testbench writes must not contaminate
        # the next case. Keep copies alongside logs for replay and inspection.
        workspace = root / "workspaces" / variant / f"case-{matrix.index(case):04d}"
        shutil.copytree(root / "snapshot", workspace)
        report["copied_bytes"] = report.get("copied_bytes", 0) + report["inputs"]["bytes"]
        if mutant is not None:
            (workspace / mutant.file).write_text(mutant.mutated)
        child_ws = Workspace(str(workspace))
        if not child_ws.config.get("targets"):
            # An inferred target uses its directory's basename. Copies have
            # different basenames but must retain the selected target identity.
            inferred = child_ws.targets.pop(child_ws.default_target)
            inferred.name = ctx.target_name
            child_ws.targets[ctx.target_name] = inferred
            child_ws.default_target = ctx.target_name
        child = ToolContext(child_ws, target=ctx.target_name, runner=runner)
        t = child.find_test(case["test"])
        if t is None:
            raise ValueError("selected test is absent from the mutation snapshot")
        conf = _runner_of(t)
        inputs = list(child.target.sources)
        for key in ("files", "rtl", "cpp", "inputs"):
            inputs.extend(ws_path(child, p)[0] for p in conf.get(key, []))
        for key in ("incdirs", "cpp_incdirs", "test_dir"):
            paths = conf.get(key, []) if key != "test_dir" else [conf.get(key, "tb")]
            for p in paths:
                ws_path(child, p)
        validate_inputs(child, inputs)
        got = _plan_for_test(child, case["test"], seed=case["seed"], waves=False,
                             timeout_s=timeout_s, output_dir=str(workspace / "mutation-build"))
        if isinstance(got, dict):
            return {**case, "status": "error", "summary": got.get("error", str(got))}
        adapter, plan, files, timeout = got
        validate_inputs(child, files)
        why = stop_reason()
        if why:
            raise LimitReached(why)
        if plan.license_features:
            ok, why = ctx.ledger.can_spend_license(0)
            if not ok:
                raise LimitReached(why)
        runner.deadline = min(deadline, time.monotonic() + timeout,
                              time.monotonic() + max(0, ctx.ledger.budget.cpu_hours * 3600 - ctx.ledger.cpu_seconds))
        if plan.license_features:
            runner.deadline = min(runner.deadline, time.monotonic() + max(0,
                ctx.ledger.budget.license_hours * 3600 - ctx.ledger.license_seconds))
        runner.license_wait_s = max(0, runner.deadline - time.monotonic())
        if ctx.runner.license_wait_s is not None:
            runner.license_wait_s = min(runner.license_wait_s, ctx.runner.license_wait_s)
        ok, why = ctx.ledger.reserve_v3()
        if not ok:
            raise LimitReached(why)
        report["jobs_submitted"] += 1
        plan.meta.update(target_name=child.target_name, verification_hash=child.verification_hash())
        save()
        rec, _ = runner.submit(plan, adapter, input_files=files, seed=case["seed"],
                              timeout=timeout, no_cache=True,
                              idle_timeout=plan.meta.get("idle_timeout_s"),
                              env_modules=child.ws.config.get("tools", {}).get("env_modules", {}).get(adapter.name, []))
        # Isolated stores use J-* too; qualify the accounting key across stores.
        ctx.ledger.record_job(replace(rec, id=campaign + "/" + rec.id))
        return {**case, "status": classify(rec, plan), "job": rec.id,
                "record": ctx.rel(root / "jobs" / rec.id / "record.json"),
                "summary": rec.summary, "workspace": ctx.rel(workspace)}

    try:
        report["inputs"] = snapshot(ctx, root / "snapshot", deadline)
        for mutant in mutants:
            if (root / "snapshot" / mutant.file).read_bytes().decode("utf-8") != mutant.original:
                raise ValueError("changed RTL moved during mutation planning; retry")
        for case in matrix:
            result = run_case(case, "baseline")
            if result["status"] == "detected":
                result["status"] = "failed"
            report["baseline"].append(result)
            save()
            if result["status"] != "passed":
                report["status"] = "baseline_failed"
                report["reason"] = "every selected test and seed must pass before mutations run"
                break
        else:
            for mutant, item in zip(mutants, report["mutants"]):
                for case in matrix:
                    result = run_case(case, mutant.id, mutant)
                    item["cases"].append(result)
                    if result["status"] == "detected":
                        item["status"] = "detected"
                        item["detected_by"] = case
                        save()
                        break
                    # Build/launch/time errors are distinct from a detected bug;
                    # other tests can still detect the same mutation.
                    item["status"] = "incomplete"
                    save()
                else:
                    statuses = {c["status"] for c in item["cases"]}
                    item["status"] = next((s for s in ("compile_error", "timeout", "error") if s in statuses), "survived")
                    save()
            report["status"] = "completed"
    except (LimitReached, TimeoutError) as exc:
        report["status"], report["reason"] = "budget_exhausted", str(exc)
    except (OSError, ValueError, PathRefused) as exc:
        report["status"], report["reason"] = "error", str(exc)
    save()
    return report


class LimitReached(Exception):
    pass
