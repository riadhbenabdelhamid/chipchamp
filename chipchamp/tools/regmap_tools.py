"""Register-map tools (SPEC §9-I, playbook P10).

``regmap.generate`` is the *sanctioned* writer for generated outputs — the agent
edits the YAML spec (via fs.edit; the spec is a source, not generated) and then
regenerates. Direct fs.write/fs.edit on the outputs is refused by FR-PROJ-04.
"""
from __future__ import annotations

import os

from ..regmap import generate, load_spec, validate
from ..util.hashing import sha256_text
from .base import tool
from .context import ToolContext


def _entry_for_spec(ctx: ToolContext, spec_path: str) -> dict | None:
    """Find the [[generated]] entry whose sources cover this spec."""
    import fnmatch
    for entry in ctx.ws.generated:
        for g in entry.get("sources", []):
            if fnmatch.fnmatch(spec_path, g):
                return entry
    return None


@tool("regmap.validate", "Validate a register-map YAML spec (field overlaps, "
      "alignment, access types, reset widths).", group="regmap",
      schema={"type": "object", "properties": {"spec": {"type": "string"}},
              "required": ["spec"]})
def regmap_validate(ctx: ToolContext, spec: str) -> dict:
    path = spec if os.path.isabs(spec) else os.path.join(ctx.ws.root, spec)
    if not os.path.exists(path):
        return {"error": f"no such spec: {spec}"}
    try:
        s = load_spec(path)
    except Exception as e:
        return {"ok": False, "errors": [f"parse error: {e}"]}
    errors = validate(s)
    return {"ok": not errors, "errors": errors,
            "registers": [f"{r.name}@0x{r.offset:03x}" for r in s.registers]}


@tool("regmap.generate", "Regenerate RTL + C header + docs from a register-map "
      "spec (deterministic; the sanctioned path for generated outputs).",
      cost="cheap", permission="write", group="regmap",
      schema={"type": "object", "properties": {
          "spec": {"type": "string"},
          "out_dir": {"type": "string",
                      "description": "defaults to the [[generated]] mapping"}},
          "required": ["spec"]})
def regmap_generate(ctx: ToolContext, spec: str, out_dir: str = "") -> dict:
    rel_spec = spec
    path = spec if os.path.isabs(spec) else os.path.join(ctx.ws.root, spec)
    if not os.path.exists(path):
        return {"error": f"no such spec: {spec}"}
    s = load_spec(path)
    errors = validate(s)
    if errors:
        return {"ok": False, "errors": errors}
    outputs = generate(s)
    entry = _entry_for_spec(ctx, rel_spec)
    written = []
    for fname, content in outputs.items():
        target = _output_path(ctx, entry, fname, out_dir)
        ap = os.path.join(ctx.ws.root, target)
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        old = open(ap, errors="replace").read() if os.path.exists(ap) else ""
        changed = old != content
        if changed:
            with open(ap, "w") as fh:
                fh.write(content)
            ctx.record_edit(target, old, content,
                            "modified" if old else "added")
        written.append({"path": target, "changed": changed,
                        "sha256": sha256_text(content)[:12]})
    ctx.ws.invalidate(ctx.target_name)
    return {"ok": True, "spec": rel_spec, "written": written,
            "note": "outputs are deterministic; commit spec + outputs together"}


def _output_path(ctx: ToolContext, entry: dict | None, fname: str,
                 out_dir: str) -> str:
    """Map a generated file to its committed location via [[generated]] outputs
    globs (match on basename pattern), else out_dir, else rtl/gen/."""
    import fnmatch
    if entry:
        for g in entry.get("outputs", []):
            if fnmatch.fnmatch(fname, os.path.basename(g)):
                return os.path.join(os.path.dirname(g), fname)
    base = out_dir or "rtl/gen"
    return os.path.join(base, fname)


def regen_check(ctx: ToolContext) -> bool | None:
    """The regmap_regen gate (SPEC §11.1): regenerate every regmap-kind
    [[generated]] entry in memory and byte-compare with the workspace. Returns
    None when no regmap entries exist (gate reports 'missing')."""
    import fnmatch
    import glob as _g
    checked = False
    for entry in ctx.ws.generated:
        if entry.get("kind") != "regmap":
            continue
        for src_glob in entry.get("sources", []):
            for src in _g.glob(os.path.join(ctx.ws.root, src_glob)):
                checked = True
                try:
                    s = load_spec(src)
                    outputs = generate(s)
                except Exception:
                    return False
                for fname, content in outputs.items():
                    target = _output_path(ctx, entry, fname, "")
                    ap = os.path.join(ctx.ws.root, target)
                    if not os.path.exists(ap):
                        return False
                    if open(ap, errors="replace").read() != content:
                        return False
    return True if checked else None
