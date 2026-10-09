"""Coverage-guided ordering and explicit gaps; this never selects fewer tests."""
from collections import defaultdict
import os

from .history import definition_hash
from .impact import location, map_region, regions, fallback_reason
from .sources import read_source


def prioritize(history, names, changes):
    from ..tools.paths import PathRefused
    ctx = history.ctx
    # Lists of paths remain supported for older internal callers, with an
    # explicit file-level fallback because they carry no before/after text.
    diff = getattr(changes, "files", None)
    paths = [c.path for c in diff] if diff is not None else list(changes)
    wanted = {os.path.realpath(os.path.join(ctx.ws.root, p)): p for p in paths}
    by_path = {c.path: c for c in diff or []}
    edits = [r for c in diff or [] for r in regions(c)]
    by_region = defaultdict(list)
    for i, region in enumerate(edits):
        by_region[region["path"]].append(i)
    hit = {n: set() for n in names}
    fallback = {n: set() for n in names}
    evidence = {n: set() for n in names}
    matched = [dict() for _ in edits]
    observations = [list() for _ in edits]
    reasons = [set() for _ in edits]
    files_fallback = defaultdict(set)
    mapping_cache, source_cache = {}, {}
    seen, used = set(), set()
    try:
        entries, invalid = history.entries()
    except PathRefused:
        entries, invalid = [], [{"reason": "coverage history is not readable"}]
    for data, rec in entries:
        name, seed = data["test"], data["seed"]
        if name not in hit or (name, seed) in seen:
            continue
        seen.add((name, seed))
        if data["definition_hash"] != definition_hash(ctx, name):
            continue
        bins_by_file = defaultdict(list)
        for key, item in data["model"]["bins"].items():
            source, line = location(item.get("source"))
            absolute = os.path.realpath(os.path.join(ctx.ws.root, source))
            if source and absolute in wanted:
                bins_by_file[wanted[absolute]].append((key, item, line))
        for path, bins in bins_by_file.items():
            fallback_here = False
            if not by_region[path]:
                reason = ("before/after source text is unavailable" if diff is None else
                          "no textual line change; retain file-level impact")
                files_fallback[path].add(reason)
                fallback_here = True
            for index in by_region[path]:
                region = edits[index]
                spec = data.get("source_snapshots", {}).get(path, {})
                source_key = (path, spec.get("path"), spec.get("sha256"))
                try:
                    if source_key not in source_cache:
                        source_cache[source_key] = read_source(ctx, data, path)
                    snapshot = source_cache[source_key]
                    digest = data["source_snapshots"][path]["sha256"]
                    map_key = (digest, path, index)
                    if map_key not in mapping_cache:
                        mapping_cache[map_key] = map_region(snapshot, by_path[path], region)
                    lines, side, reason = mapping_cache[map_key]
                    if reason:
                        raise ValueError(reason)
                    selected = [(key, item, line) for key, item, line in bins if line in lines]
                    if not selected:
                        raise ValueError("no recorded bins at the mapped lines; instrumentation is unknown")
                    used.add(index)
                    for key, item, line in selected:
                        identity = (digest, key)
                        covered = item.get("count", 0) > 0
                        matched[index][identity] = matched[index].get(identity, False) or covered
                        if covered:
                            hit[name].add(identity)
                            evidence[name].add(rec.id)
                    observations[index].append({"test": name, "seed": seed, "job": rec.id,
                        "source_sha256": digest, "side": side,
                        "recorded_lines": sorted({line for _, _, line in selected}),
                        "hit_bins": sum(item.get("count", 0) > 0 for _, item, _ in selected),
                        "bins": [key for key, _, _ in selected][:50], "bin_count": len(selected)})
                except (OSError, ValueError, KeyError, PathRefused) as error:
                    reasons[index].add(str(error))
                    files_fallback[path].add(str(error))
                    fallback_here = True
            if fallback_here:
                for key, item, _ in bins:
                    if item.get("count", 0) > 0:
                        fallback[name].add(key)
                        evidence[name].add(rec.id)
    for i, region in enumerate(edits):
        n_hit = sum(matched[i].values())
        n_unhit = len(matched[i]) - n_hit
        if i not in used:
            reason = fallback_reason(region["path"], by_path[region["path"]].old_text,
                                     by_path[region["path"]].new_text)
            reasons[i].add(reason or "no compatible observation maps to these changed lines")
            files_fallback[region["path"]].update(reasons[i])
        region.update(status="partial_hits" if n_hit and n_unhit else "historical_hits" if n_hit else
                      "uncovered_bins" if n_unhit else "unknown",
                      hit_bins=n_hit, unhit_bins=n_unhit,
                      tests=sorted({e["test"] for e in observations[i] if e["hit_bins"]}),
                      evidence=observations[i], fallback_reasons=sorted(reasons[i]))
    tests = sorted(names, key=lambda name: (-len(hit[name]), -len(fallback[name])))
    has_lines, has_files = any(hit.values()), any(fallback.values())
    return {"method": "coverage_lines" if has_lines else "coverage_history" if has_files else "declared_order",
            "tests": tests, "reasons": {n: {"changed_bins_hit": len(hit[n]),
                "file_bins_hit": len(fallback[n]), "jobs": sorted(evidence[n])} for n in names},
            "impact": {"scope": "selected_tests", "regions": edits, "mapped_regions": len(used),
                "unknown_regions": len(edits) - len(used),
                "fallbacks": [{"path": path, "reasons": sorted(why)} for path, why in sorted(files_fallback.items())]},
            "warnings": [f"Ignored {len(invalid)} invalid or unreadable coverage observation(s)."] if invalid else [],
            "note": "Historical hits identify relevant tests, not coverage of edited logic. Every selected test and seed still runs."}
