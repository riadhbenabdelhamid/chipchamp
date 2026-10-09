"""Relate recorded coverage locations to requested edits without guessing offsets."""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import re


MAX_SOURCE_BYTES = 2_000_000
MAX_LINES = 20_000


def location(source):
    match = re.fullmatch(r"(.+):(\d+)", source or "")
    return (match[1], int(match[2])) if match and int(match[2]) > 0 else (source or "", None)


def fallback_reason(path, *texts):
    if Path(path).suffix.lower() not in (".sv", ".v"):
        return "header or non-RTL file; effects may extend beyond source lines"
    if any(p.lower() in ("generated", "gen", "autogen", "obj_dir") for p in Path(path).parts):
        return "generated source requires file-level impact"
    for text in texts:
        if len(text.encode("utf-8")) > MAX_SOURCE_BYTES or len(text.splitlines()) > MAX_LINES:
            return "source exceeds the line-mapping limit"
        if re.search(r"(?i)\b(?:auto.?generated|generated (?:by|file)|do not edit)\b", text[:2000]):
            return "generated source requires file-level impact"
        if re.search(r"`(?!timescale\b|default_nettype\b)[A-Za-z_]\w*", text):
            return "macro/include/preprocessor locations require file-level impact"
    return ""


def regions(change):
    """Inclusive old/new ranges, with [] for the absent side of an edit."""
    old, new = change.old_text.splitlines(), change.new_text.splitlines()
    if max(len(old), len(new)) > MAX_LINES:
        edits = [("replace", 0, len(old), 0, len(new))]
    else:
        edits = SequenceMatcher(None, old, new).get_opcodes()
    return [{"path": change.path, "change": tag,
             "old_lines": [a + 1, b] if b > a else [],
             "new_lines": [c + 1, d] if d > c else []}
            for tag, a, b, c, d in edits if tag != "equal"]


def recorded_lines(snapshot, reference, span):
    """Map an edit's lines back to the recorded source using unchanged text only.

    Repeated lines across different versions are deliberately ambiguous. Direct
    byte-identical versions need no alignment and retain their exact line numbers.
    """
    if not span:
        return set(), "this edit has no lines in the recorded side"
    if snapshot == reference:
        return set(range(span[0], span[1] + 1)), ""
    old, new = snapshot.splitlines(), reference.splitlines()
    if max(len(old), len(new)) > MAX_LINES:
        return set(), "source exceeds the line-mapping limit"
    counts_old, counts_new = Counter(old), Counter(new)
    mapping = {}
    for block in SequenceMatcher(None, old, new).get_matching_blocks():
        for offset in range(block.size):
            line = old[block.a + offset]
            if line.strip() and counts_old[line] == counts_new[line] == 1:
                mapping[block.b + offset + 1] = block.a + offset + 1
    wanted = set(range(span[0], span[1] + 1))
    found = {mapping[n] for n in wanted if n in mapping}
    return found, "historical lines changed or have ambiguous repeated text" if wanted - mapping.keys() else ""


def map_region(snapshot, change, region):
    reason = fallback_reason(change.path, snapshot, change.old_text, change.new_text)
    if reason:
        return set(), "", reason
    # Prefer the exact recorded version, including a new observation collected
    # after an edit. Never interpret an old line number as a current line number.
    sides = [("old", change.old_text), ("new", change.new_text)]
    sides.sort(key=lambda side: snapshot != side[1])
    for side, reference in sides:
        span = region[side + "_lines"]
        mapped, reason = recorded_lines(snapshot, reference, span)
        if mapped and not reason:
            return mapped, side, ""
    return set(), "", "new, deleted or changed lines cannot be aligned reliably to this observation"


def explain(ordering, limit=20):
    """Human-readable evidence shared by CLI output and saved Markdown reports."""
    if not ordering:
        return []
    output = []
    if ordering["method"] != "declared_order":
        mode = "changed lines" if ordering["method"] == "coverage_lines" else "file fallback"
        output.append(f"Coverage order ({mode}): " + ", ".join(ordering["tests"]))
    impact = ordering.get("impact", {})
    edits = impact.get("regions", [])
    if edits:
        output.append(f"Changed regions: {impact['mapped_regions']} mapped, {impact['unknown_regions']} unknown.")
    def span(values):
        return "none" if not values else str(values[0]) if values[0] == values[1] else f"{values[0]}-{values[1]}"
    for region in edits[:limit]:
        jobs = sorted({e["job"] for e in region["evidence"]})
        output.append(f"{region['path']} old {span(region['old_lines'])} -> new {span(region['new_lines'])}: "
                      f"{region['status'].replace('_', ' ')}; {region['hit_bins']} hit / {region['unhit_bins']} unhit bins; "
                      f"tests: {', '.join(region['tests']) or 'none established'}; jobs: {', '.join(jobs) or 'none'}")
    if len(edits) > limit:
        output.append(f"{len(edits) - limit} more changed regions are available in the JSON report.")
    for item in impact.get("fallbacks", [])[:limit]:
        output.append(f"File fallback for {item['path']}: {'; '.join(item['reasons'])}.")
    if edits or impact.get("fallbacks"):
        output.append(ordering["note"])
    return output
