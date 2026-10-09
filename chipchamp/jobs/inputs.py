"""Conservative input capture for cache reuse and verification freshness.

Literal HDL/C includes and memory images are followed transitively. Dynamic
filenames, shell wrappers and unreadable dependencies make reuse ineligible;
they must never silently turn an incomplete manifest into a cache hit.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..util.hashing import sha256_file


def capture_inputs(plan, input_files=()) -> dict:
    roots = set()
    incdirs = []
    workdirs = []
    complete = True
    outputs = {os.path.realpath(os.path.join(plan.workdir, p))
               for p in plan.artifacts.values() if p}
    for f in input_files:
        roots.add(os.path.realpath(os.path.join(plan.workdir, f)))
    for step in plan.steps:
        cwd = os.path.realpath(step.cwd or plan.workdir)
        workdirs.append(cwd)
        argv = step.argv
        if argv and os.path.basename(argv[0]) in ("sh", "bash", "csh", "tcsh"):
            complete = False
        for i, arg in enumerate(argv):
            if arg == "-I" and i + 1 < len(argv):
                incdirs.append(os.path.realpath(os.path.join(cwd, argv[i + 1])))
            elif arg.startswith("-I") and len(arg) > 2:
                incdirs.append(os.path.realpath(os.path.join(cwd, arg[2:])))
            elif arg.startswith("+incdir+"):
                incdirs.extend(os.path.realpath(os.path.join(cwd, d))
                               for d in arg[len("+incdir+"):].split("+") if d)
            path = os.path.realpath(os.path.join(cwd, arg))
            if path not in outputs and Path(path).is_file():
                roots.add(path)
            if arg in ("-f", "-F"):
                # Filelists may themselves contain options and nested lists.
                complete = False
    if plan.adapter == "cocotb":
        complete = False  # Python imports/data need an adapter-provided manifest.
    files, resolved = dependency_files(roots, incdirs, workdirs, outputs)
    return {"files": files, "complete": complete and resolved,
            "roots": sorted(roots), "incdirs": incdirs, "workdirs": workdirs}


def dependency_files(roots, incdirs=(), workdirs=(), outputs=()) -> tuple[dict, bool]:
    pending = list(roots)
    files = {}
    complete = True
    text_exts = {".sv", ".svh", ".v", ".vh", ".c", ".cc", ".cpp", ".h", ".hpp"}
    while pending:
        path = os.path.realpath(pending.pop())
        if path in files or path in outputs:
            continue
        try:
            files[path] = sha256_file(path)
            if Path(path).suffix.lower() not in text_exts:
                continue
            text = Path(path).read_text(errors="replace")
        except OSError:
            files[path] = "MISSING"
            complete = False
            continue
        # Comments must not manufacture dependencies; preserve quoted strings.
        text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                      lambda m: m[0] if m[0].startswith('"') else " ", text, flags=re.S)
        for m in re.finditer(r'(?:`include\s+|^\s*#\s*include\s+)([^\n]+)', text, re.M):
            lit = re.match(r'["<]([^">]+)[">]', m[1].strip())
            if not lit:
                complete = False
                continue
            dirs = [str(Path(path).parent), *incdirs, *workdirs]
            matches = [os.path.realpath(os.path.join(d, lit[1])) for d in dirs]
            found = [p for p in matches if Path(p).is_file()]
            if not found:
                complete = False
            else:
                # Tool-specific search precedence varies. Hash every possible
                # match rather than risk pinning a different header's bytes.
                pending.extend(found)
        for m in re.finditer(r'\$readmem[hb]\s*\(\s*([^,]+)', text):
            lit = re.fullmatch(r'"([^"\n]+)"\s*', m[1])
            if not lit:
                complete = False
                continue
            paths = [os.path.realpath(os.path.join(d, lit[1])) for d in workdirs]
            found = [p for p in paths if Path(p).is_file()]
            if not found:
                complete = False
            pending.extend(found)
    return files, complete


def snapshot_current(snapshot: dict) -> bool:
    files, _ = dependency_files(snapshot.get("roots", []), snapshot.get("incdirs", []),
                                snapshot.get("workdirs", []))
    return files == snapshot.get("files", {})
