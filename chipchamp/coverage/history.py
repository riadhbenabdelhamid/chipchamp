"""Immutable per-job coverage observations, validated against runner provenance."""
from __future__ import annotations

from pathlib import Path

from ..jobs.inputs import snapshot_current
from ..util.hashing import sha256_file, sha256_text
from ..util.jsonio import atomic_write, dump_json, load_json
from .model import CoverageModel
from .service import CoverageService


def definition_hash(ctx, test):
    # Deliberately independent of source bytes: historical hits can help order
    # tests after an RTL edit, but never stand in for current verification.
    return sha256_text(dump_json({"test": ctx.find_test(test), "target": ctx.target_name,
        "defines": ctx.target.defines, "incdirs": ctx.target.incdirs,
        "tools": ctx.ws.config.get("tools", {})}, sort_keys=True))


class CoverageHistory:
    def __init__(self, ctx):
        self.ctx = ctx
        self.directory = ctx.ws.dot / "coverage" / "history"

    def record(self, rec, model, artifact):
        from ..tools.paths import ws_path
        if rec.coverage_observation:
            raise ValueError("this job already has an immutable coverage observation")
        if (rec.status != "passed" or not model.bins or not rec.test_name
                or rec.seed is None or not snapshot_current(rec.input_snapshot)
                or rec.evidence_hashes.get(artifact) != sha256_file(artifact)):
            raise ValueError("coverage requires a passed job, nonempty bins and unchanged inputs/artifact")
        path, relative = ws_path(self.ctx, self.ctx.rel(self.directory / f"{rec.id}.json"), write=True)
        from .sources import capture_sources
        sources, unavailable = capture_sources(self.ctx, rec, model)
        data = {"schema_version": 2, "job": rec.id, "target": rec.target_name,
                "test": rec.test_name, "seed": rec.seed, "timestamp": rec.end_ts,
                "adapter": rec.adapter, "tool_version": rec.tool_version,
                "definition_hash": definition_hash(self.ctx, rec.test_name),
                "verification_hash": rec.verification_hash, "inputs_hash": rec.inputs_hash,
                "artifact": artifact, "model": model.to_dict(),
                "summary": CoverageService(model).summary(),
                "source_snapshots": sources, "source_snapshot_warnings": unavailable}
        atomic_write(path, dump_json(data))
        rec.coverage_observation = {"path": relative, "sha256": sha256_file(path)}
        self.ctx.runner._persist(rec)
        return self.describe(data, rec)

    def load(self, job):
        from ..tools.paths import safe_name, ws_path
        if not safe_name(job) or not str(job).startswith("J-"):
            raise ValueError("expected a coverage job id, e.g. J-0001")
        rec = self.ctx.runner.get(job)
        if not rec or rec.status != "passed" or rec.target_name != self.ctx.target_name:
            raise ValueError(f"no passed coverage observation for {job} on this target")
        pointer = rec.coverage_observation
        if not pointer:
            raise ValueError(f"no coverage observation for {job}")
        path = ws_path(self.ctx, pointer["path"])[0]
        if sha256_file(path) != pointer["sha256"]:
            raise ValueError(f"coverage observation for {job} was modified")
        data = load_json(path)
        if (data.get("schema_version") not in (1, 2) or data.get("job") != job
                or data.get("test") != rec.test_name or data.get("seed") != rec.seed
                or data.get("target") != rec.target_name):
            raise ValueError(f"invalid coverage observation for {job}")
        artifact = data["artifact"]
        if rec.evidence_hashes.get(artifact) != sha256_file(artifact):
            raise ValueError(f"coverage artifact for {job} is missing or changed")
        for step in rec.steps:
            if rec.evidence_hashes.get(step.log_path) != sha256_file(step.log_path):
                raise ValueError(f"coverage log for {job} is missing or changed")
        return data, rec

    def describe(self, data, rec):
        current = (rec.verification_hash == self.ctx.verification_hash()
                   and snapshot_current(rec.input_snapshot))
        return {k: data[k] for k in ("job", "target", "test", "seed", "timestamp",
                 "adapter", "tool_version", "inputs_hash", "summary")} | {
                     "current": current,
                     "compatible": data["definition_hash"] == definition_hash(self.ctx, rec.test_name)}

    def entries(self, test="", seed=None):
        from ..tools.paths import PathRefused, ws_path
        ws_path(self.ctx, self.ctx.rel(self.directory))
        entries, invalid = [], []
        for path in self.directory.glob("J-*.json"):
            try:
                record = self.ctx.runner.get(path.stem)
                if record and record.target_name != self.ctx.target_name:
                    continue
                data, rec = self.load(path.stem)
                if (not test or data["test"] == test) and (seed is None or data["seed"] == seed):
                    entries.append((data, rec))
            except (OSError, ValueError, KeyError, TypeError, PathRefused) as e:
                invalid.append({"job": path.stem, "reason": str(e)})
        entries.sort(key=lambda pair: (pair[0]["timestamp"], pair[0]["job"]), reverse=True)
        return entries, invalid

    def compare(self, before, after):
        a, _ = self.load(before)
        b, _ = self.load(after)
        if (a["target"], a["test"], a["seed"], a["adapter"]) != (
                b["target"], b["test"], b["seed"], b["adapter"]):
            raise ValueError("compare observations of the same target, test, seed and adapter")
        old, new = CoverageModel.from_dict(a["model"]), CoverageModel.from_dict(b["model"])
        common = old.bins.keys() & new.bins.keys()
        groups = {
            "newly_covered": sorted(k for k in common if new.bins[k].hit and not old.bins[k].hit),
            "newly_uncovered": sorted(k for k in common if old.bins[k].hit and not new.bins[k].hit),
            "added_bins": sorted(new.bins.keys() - old.bins.keys()),
            "removed_bins": sorted(old.bins.keys() - new.bins.keys())}
        return {"before": before, "after": after, "test": b["test"], "seed": b["seed"],
                "from_percent": old.total(), "to_percent": new.total(),
                "delta_percent": round(new.total() - old.total(), 2),
                "definition_changed": a["definition_hash"] != b["definition_hash"],
                "tool_changed": a["tool_version"] != b["tool_version"],
                **{k: {"count": len(v), "bins": v[:50]} for k, v in groups.items()},
                "baseline_check": CoverageService(new).baseline_check(old).__dict__}

    def order(self, names, changes):
        from .prioritize import prioritize
        return prioritize(self, names, changes)
