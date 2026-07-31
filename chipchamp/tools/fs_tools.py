"""Workspace file tools (SPEC §9 group A), ACL- and policy-gated.

Reads are refused for ACL-denied paths *in the tool layer* (below the model, so a
jailbreak can't exfiltrate PDK IP — FR-SEC-02). Writes are refused for protected
and denied paths, and every write is recorded so the policy engine can
reconstruct the change set for gating.
"""
from __future__ import annotations

import os
import re

from .base import tool, truncate
from .context import ToolContext


def _abs(ctx: ToolContext, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ctx.ws.root, path)


# What "source files" means when no pattern is given. The old default was
# *.sv alone — on a workspace whose own RTL is all SystemVerilog that looks
# fine forever, but it made the entire eFPGA vertical (Verilog .v, fabric
# .csv/.list, generated .txt graphs) INVISIBLE: a live agent swept the tree
# with default fs.list/fs.grep, got clean empty results, and concluded the
# project it was asked to debug did not exist.
_SOURCE_EXTS = (".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl", ".csv", ".list",
                ".f", ".core", ".tcl", ".sdc", ".xdc", ".pcf", ".lpf",
                ".yaml", ".yml", ".toml", ".md", ".txt")


def _source_files(root: str) -> list[str]:
    import glob as _g
    hits: list[str] = []
    for ext in _SOURCE_EXTS:
        hits.extend(_g.glob(os.path.join(root, "**", "*" + ext),
                            recursive=True))
    return hits


def _near_paths(ctx: ToolContext, path: str, limit: int = 3) -> list[str]:
    """Readable workspace files sharing the missing path's basename — the
    fs.read teaching hint (same class as job.log's: a mute miss on a tool
    that takes an identifier feeds a guessing loop; a miss that names the
    real candidates ends it)."""
    want = os.path.basename(path)
    out = []
    for cur, dirs, files in os.walk(ctx.ws.root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if want in files:
            rel = ctx.rel(os.path.join(cur, want))
            if ctx.policy.can_read(rel):
                out.append(rel)
                if len(out) >= limit:
                    break
    return out


@tool("fs.read", "Read a source file (ACL-enforced).", group="workspace",
      schema={"type": "object", "properties": {
          "path": {"type": "string"},
          "start": {"type": "integer"}, "end": {"type": "integer"}},
          "required": ["path"]})
def fs_read(ctx: ToolContext, path: str, start: int = 1, end: int = 0) -> dict:
    if not ctx.policy.can_read(path):
        return {"error": f"read denied: {ctx.policy.acl.reason(path)}"}
    ap = _abs(ctx, path)
    if not os.path.exists(ap):
        near = _near_paths(ctx, path)
        return {"error": f"no such file: {path}"
                + (f" — did you mean: {', '.join(near)}?" if near else ""),
                "hint": "paths are relative to the workspace root "
                        "(e.g. rtl/soc_top.sv); fs.list shows the tree"}
    lines = open(ap, errors="replace").read().splitlines()
    end = end or len(lines)
    chunk = lines[max(0, start - 1):end]
    numbered = "\n".join(f"{i+start:5d}  {ln}" for i, ln in enumerate(chunk))
    return truncate({"path": path, "lines": f"{start}-{start+len(chunk)-1}",
                     "content": numbered}, max_str=8000)


@tool("fs.list", "List source files under a directory (ACL-filtered).",
      group="workspace",
      schema={"type": "object", "properties": {
          "dir": {"type": "string", "default": "."},
          "pattern": {"type": "string", "description":
                      "glob filename pattern (e.g. *.v); default: every "
                      "source/fabric file type"}}})
def fs_list(ctx: ToolContext, dir: str = ".", pattern: str = "") -> dict:
    import glob
    base = _abs(ctx, dir)
    if not os.path.isdir(base):
        return {"error": f"no such directory: {dir}"}
    if pattern:
        hits = glob.glob(os.path.join(base, "**", pattern), recursive=True)
    else:
        hits = _source_files(base)
    rels = [ctx.rel(h) for h in hits if ctx.policy.can_read(ctx.rel(h))]
    out = {"dir": dir, "files": sorted(rels)}
    if not rels:
        # an empty listing of a NON-empty directory must say so — "[]" reads
        # as "nothing here" when the truth is "your filter excluded it"
        n = sum(len(fs) for _, _, fs in os.walk(base))
        if n:
            out["note"] = (f"directory holds {n} file(s), but none matched "
                           + (f"pattern {pattern!r}" if pattern
                              else "the default source-file extensions")
                           + " — pass pattern='*' to list everything")
    return truncate(out)


@tool("fs.grep", "Regex search across source files (ACL-filtered).", group="workspace",
      schema={"type": "object", "properties": {
          "pattern": {"type": "string"}, "glob": {"type": "string", "description":
              "glob to restrict the file set (e.g. **/*.v); default: every "
              "source/fabric file type"}},
          "required": ["pattern"]})
def fs_grep(ctx: ToolContext, pattern: str, glob: str = "") -> dict:
    import glob as _g
    rx = re.compile(pattern)
    files = (_g.glob(os.path.join(ctx.ws.root, glob), recursive=True)
             if glob else _source_files(ctx.ws.root))
    hits = []
    for f in files:
        rel = ctx.rel(f)
        if not ctx.policy.can_read(rel):
            continue
        try:
            for i, ln in enumerate(open(f, errors="replace"), 1):
                if rx.search(ln):
                    hits.append(f"{rel}:{i}: {ln.rstrip()}")
        except OSError:
            continue
    return truncate({"pattern": pattern, "hits": hits}, max_items=40)


@tool("fs.write", "Create or overwrite a file (policy/ACL-gated; recorded for "
      "gating). Refuses generated outputs and protected paths.", permission="write",
      group="workspace",
      schema={"type": "object", "properties": {
          "path": {"type": "string"}, "content": {"type": "string"}},
          "required": ["path", "content"]})
def fs_write(ctx: ToolContext, path: str, content: str) -> dict:
    ok, why = ctx.policy.can_write(path)
    if not ok:
        return {"error": f"write denied: {why}"}
    ap = _abs(ctx, path)
    old = open(ap, errors="replace").read() if os.path.exists(ap) else ""
    status = "modified" if old else "added"
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    with open(ap, "w") as fh:
        fh.write(content)
    ctx.record_edit(path, old, content, status)
    ctx.ws.invalidate(ctx.target_name)  # design DB + live source glob
    out = {"path": path, "status": status, "bytes": len(content)}
    # A rewrite that shrinks a substantial file is usually an EDIT that
    # went through fs.write from memory — a live agent replaced fabric.csv
    # with a from-imagination half-length version and lost the Parameters
    # section it never meant to touch. Say so; do not block.
    old_n, new_n = old.count("\n"), content.count("\n")
    if old_n >= 20 and new_n < old_n // 2:
        out["note"] = (f"overwrote {path}: {old_n} → {new_n} lines. If you "
                       f"meant to CHANGE part of this file, fs.edit(old=..., "
                       f"new=...) preserves everything you did not name; a "
                       f"full rewrite must reproduce the whole file exactly.")
    return out


def _strip_render_prefix(lines: list[str]) -> list[str] | None:
    """If EVERY line starts with a line-number prefix (a pasted fs.read
    numbered render), strip it; else None."""
    import re as _re
    out = []
    for ln in lines:
        m = _re.match(r"^\s*\d+\s{2}(.*)$", ln)
        if not m:
            return None
        out.append(m.group(1))
    return out


def _lenient_find(file_lines: list[str], old_lines: list[str]) -> list[int]:
    """Start indices where old matches file ignoring per-line leading/trailing
    whitespace."""
    want = [ln.strip() for ln in old_lines]
    hits = []
    for i in range(len(file_lines) - len(want) + 1):
        if [file_lines[i + j].strip() for j in range(len(want))] == want:
            hits.append(i)
    return hits


def _apply_lenient(file_lines: list[str], start: int, n_old: int,
                   old_lines: list[str], new: str) -> list[str]:
    """Replace the matched window, re-indenting `new` by the indentation delta
    between the file's first matched line and the caller's first old line."""
    file_indent = len(file_lines[start]) - len(file_lines[start].lstrip())
    old_indent = len(old_lines[0]) - len(old_lines[0].lstrip())
    delta = file_indent - old_indent
    new_lines = []
    for ln in new.split("\n"):
        if not ln.strip():
            new_lines.append("")
        elif delta >= 0:
            new_lines.append(" " * delta + ln)
        else:
            cur = len(ln) - len(ln.lstrip())
            new_lines.append(ln[min(-delta, cur):])
    return file_lines[:start] + new_lines + file_lines[start + n_old:]


def _nearest_raw(text: str, old: str, window: int = 2) -> dict | None:
    """The raw file bytes around the line closest to the first old line —
    so a model can retry with an exact match."""
    import difflib
    first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
    if not first:
        return None
    lines = text.split("\n")
    close = difflib.get_close_matches(first, [ln.strip() for ln in lines],
                                      n=1, cutoff=0.4)
    if not close:
        return None
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == close[0])
    lo, hi = max(0, idx - window), min(len(lines), idx + window + 1)
    return {"line": idx + 1, "raw": "\n".join(lines[lo:hi])}


@tool("fs.delete", "Delete a file (DESTRUCTIVE — irreversible; always requires "
      "explicit human confirmation, in every autonomy mode). Recorded for "
      "gating. Prefer leaving a stale file over deleting; never delete files "
      "you did not create for this task.", permission="write",
      destructive=True, group="workspace",
      schema={"type": "object", "properties": {
          "path": {"type": "string"}}, "required": ["path"]})
def fs_delete(ctx: ToolContext, path: str) -> dict:
    ok, why = ctx.policy.can_write(path)
    if not ok:
        return {"error": f"delete denied: {why}"}
    ap = _abs(ctx, path)
    if not os.path.exists(ap):
        return {"error": f"no such file: {path}"}
    if os.path.isdir(ap):
        return {"error": f"{path} is a directory (fs.delete removes single files)"}
    old = open(ap, errors="replace").read()
    os.remove(ap)
    ctx.record_edit(path, old, "", "deleted")
    ctx.ws.invalidate(ctx.target_name)
    return {"path": path, "status": "deleted", "bytes": len(old)}


@tool("fs.edit", "Exact string replacement in a file (policy/ACL-gated; "
      "recorded). `old` must match the file's RAW bytes — fs.read's line "
      "numbers are a display prefix, not file content. Whitespace-lenient "
      "fallback applies when the match is unique.",
      permission="write", group="workspace",
      schema={"type": "object", "properties": {
          "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
          "required": ["path", "old", "new"]})
def fs_edit(ctx: ToolContext, path: str, old: str, new: str) -> dict:
    ok, why = ctx.policy.can_write(path)
    if not ok:
        return {"error": f"write denied: {why}"}
    ap = _abs(ctx, path)
    if not os.path.exists(ap):
        return {"error": f"no such file: {path}"}
    text = open(ap, errors="replace").read()
    baseline = ctx.edits.get(path, {}).get("old", text)

    def commit(updated: str, match: str) -> dict:
        with open(ap, "w") as fh:
            fh.write(updated)
        ctx.record_edit(path, baseline, updated, "modified")
        ctx.ws.invalidate(ctx.target_name)
        return {"path": path, "status": "modified", "replaced": 1,
                "match": match}

    if text.count(old) == 1:
        return commit(text.replace(old, new, 1), "exact")
    if text.count(old) > 1:
        return {"error": f"old string is not unique ({text.count(old)} matches)"}

    # Not found verbatim. Models copy from fs.read's numbered render and lose
    # exact indentation (or keep the number prefix) — recover when the intent
    # is unambiguous, fail with the raw bytes when it isn't.
    file_lines = text.split("\n")
    for prep, label in ((old.split("\n"), "whitespace-lenient"),
                        (_strip_render_prefix(old.split("\n")),
                         "line-numbers-stripped")):
        if not prep or not any(ln.strip() for ln in prep):
            continue
        hits = _lenient_find(file_lines, prep)
        if len(hits) == 1:
            updated = "\n".join(_apply_lenient(file_lines, hits[0], len(prep),
                                               prep, new))
            return commit(updated, label)
        if len(hits) > 1:
            return {"error": f"old string is not unique even ignoring "
                             f"whitespace ({len(hits)} matches) — include "
                             f"more surrounding lines"}
    out = {"error": "old string not found (must match the file's raw bytes "
                    "exactly — fs.read line numbers and their indentation "
                    "are display-only, not file content)"}
    near = _nearest_raw(text, old)
    if near:
        out["nearest_match"] = near
        out["hint"] = ("closest raw text shown in nearest_match.raw — copy it "
                       "verbatim as `old`")
    return out
