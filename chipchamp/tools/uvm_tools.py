"""UVM tools (SPEC §9-B): scaffold a runnable UVM bench from the design DB.

``uvm.scaffold`` is a sanctioned generator like ``regmap.generate``: files are
written directly and recorded as edits (the human sees the diff); the test
manifest gains a ``uvm``-kind entry so ``sim.run`` can run the bench at once —
on the license-free Verilator+uvm-core path or on Questa/VCS unchanged.
"""
from __future__ import annotations

import os

from ..util.hashing import sha256_text
from ..uvmgen import scaffold_uvm
from .. import brand
from .base import tool
from .context import ToolContext


@tool("uvm.scaffold", "Generate a complete runnable UVM bench for a module "
      "(interface, seq_item, driver, monitor, agent, env, scoreboard stub, "
      "sequences, base+smoke test, tb top, filelist) and register it in the "
      "test manifest. The scoreboard reference model is left TODO — that is "
      "the DV work, not the plumbing.", cost="cheap", permission="write",
      group="uvm",
      schema={"type": "object", "properties": {
          "module": {"type": "string"},
          "out_dir": {"type": "string",
                      "description": "bench dir (default verif/uvm/<module>)"},
          "add_test": {"type": "boolean", "default": True,
                       "description": "append a uvm-kind entry to tests.yaml"}},
          "required": ["module"]})
def uvm_scaffold(ctx: ToolContext, module: str, out_dir: str = "",
                 add_test: bool = True) -> dict:
    mod = ctx.db.module(module)
    if not mod:
        return {"error": f"module '{module}' not in index",
                "known": sorted(ctx.db.modules)[:30]}
    base = out_dir or os.path.join("verif", "uvm", module)
    files = scaffold_uvm(mod)
    written = []
    for fname, content in files.items():
        target = os.path.join(base, fname)
        ap = os.path.join(ctx.ws.root, target)
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        old = open(ap, errors="replace").read() if os.path.exists(ap) else ""
        if old != content:
            with open(ap, "w") as fh:
                fh.write(content)
            ctx.record_edit(target, old, content, "modified" if old else "added")
        written.append({"path": target, "sha256": sha256_text(content)[:12]})

    test_name = f"{module}_uvm_smoke"
    entry = {
        "name": test_name,
        "target": ctx.target_name,
        "runner": {
            "kind": "uvm",
            "tb_top": f"tb_{module}_uvm",
            "files": [os.path.join(base, f"{module}_pkg.sv"),
                      os.path.join(base, f"tb_{module}_uvm.sv")],
            "incdirs": [base],
            "uvm_test": f"{module}_smoke_test",
        },
        "tags": ["uvm", "smoke", module],
        "owner": brand.APP_NAME,
    }
    manifest_note = "not added (add_test=false)"
    if add_test:
        import yaml
        tpath = ctx.ws.dot / "tests.yaml"
        old = tpath.read_text() if tpath.exists() else ""
        new = _append_test_entry(old, entry, test_name)
        tpath.write_text(new)
        ctx.record_edit(str(tpath.relative_to(ctx.ws.root)), old, new,
                        "modified" if old else "added")
        tests = [t for t in ctx.ws.tests if t.get("name") != test_name]
        tests.append(entry)
        ctx.ws.tests = tests  # keep the live manifest in sync
        manifest_note = f"tests.yaml: {test_name}"

    return {"module": module, "bench_dir": base,
            "files": written, "manifest": manifest_note,
            "run_with": {"tool": "sim.run",
                         "args": {"test": test_name}},
            "note": "scoreboard reference model is TODO — wire the DUT's "
                    "expected behavior, then climb the ladder"}


def _append_test_entry(old: str, entry: dict, test_name: str) -> str:
    """Append a test to tests.yaml WITHOUT round-tripping the whole file
    (a parse+dump would strip the user's comments/formatting). The entry is
    appended as a marker-delimited block at the file's own list indentation;
    only if the result doesn't parse back cleanly do we fall back to the
    lossy structured rewrite."""
    import re

    import yaml
    marker = f"# --- {brand.APP_NAME} uvm: {test_name} ---"
    endmark = f"# --- end {brand.APP_NAME} uvm ---"
    # regeneration: strip our previous block textually — under ANY brand era,
    # so a tests.yaml written before a rename regenerates instead of doubling
    for _era in brand.all_names():
        _mk = f"# --- {_era} uvm: {test_name} ---"
        _em = f"# --- end {_era} uvm ---"
        if _mk in old:
            pre, rest = old.split(_mk, 1)
            rest = rest.split(_em, 1)[-1].lstrip("\n")
            old = pre.rstrip() + "\n" + (rest if rest.strip() else "")
    # match the existing entries' indentation (or 2 spaces for a fresh list)
    im = re.search(r"(?m)^(\s*)- name:", old)
    indent = im.group(1) if im else "  "
    block = yaml.safe_dump([entry], sort_keys=False, default_flow_style=False)
    indented = "".join(indent + ln if ln.strip() else ln
                       for ln in block.splitlines(keepends=True))
    head = old.rstrip() + "\n" if old.strip() else ""
    if "tests:" not in old:
        head += "tests:\n"
    candidate = (head + indent + marker + "\n" + indented
                 + indent + endmark + "\n")
    try:
        parsed = yaml.safe_load(candidate)
        entries = parsed.get("tests", []) if isinstance(parsed, dict) else []
        names = [t.get("name") for t in entries if isinstance(t, dict)]
        if names.count(test_name) == 1:
            return candidate
    except yaml.YAMLError:
        pass
    # Lossy fallback: structured rewrite. Tolerate the same hand-written
    # shapes the workspace loader accepts (bare top-level list, scalar
    # entries, invalid YAML — see config._load_tests): coerce to the
    # canonical mapping instead of crashing on .setdefault.
    try:
        data = (yaml.safe_load(old) or {}) if old.strip() else {}
    except yaml.YAMLError:
        data = {}
    if isinstance(data, list):
        data = {"tests": data}
    elif not isinstance(data, dict):
        data = {}
    tests = data.setdefault("tests", [])
    if not isinstance(tests, list):
        tests = data["tests"] = []
    tests[:] = [t for t in tests
                if isinstance(t, dict) and t.get("name") != test_name]
    tests.append(entry)
    return yaml.safe_dump(data, sort_keys=False)
