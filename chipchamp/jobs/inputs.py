"""Conservative input capture for cache reuse and verification freshness.

Literal HDL/C includes and memory images are followed transitively. Dynamic
filenames, shell wrappers and unreadable dependencies make reuse ineligible;
they must never silently turn an incomplete manifest into a cache hit.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from ..util.hashing import sha256_file


def capture_inputs(plan, input_files=()) -> dict:
    roots = set()
    incdirs = []
    workdirs = []
    complete = True
    reasons = []
    directories = list(plan.meta.get("input_dirs", []))
    outputs = {os.path.realpath(os.path.join(plan.workdir, p))
               for p in plan.artifacts.values() if p}
    outputs.update(os.path.realpath(p["path"]) for p in plan.meta.get("depfiles", []))
    for f in [*input_files, *plan.meta.get("input_files", [])]:
        roots.add(os.path.realpath(os.path.join(plan.workdir, f)))
    for step in plan.steps:
        cwd = os.path.realpath(step.cwd or plan.workdir)
        workdirs.append(cwd)
        argv = step.argv
        if argv and os.path.basename(argv[0]) in ("sh", "bash", "csh", "tcsh"):
            complete = False
            reasons.append("shell wrapper has undeclared inputs")
        if plan.adapter == "icarus" and any(a == "-m" or a.startswith("-m") for a in argv[1:]):
            complete = False
            reasons.append("VPI module dependencies require runtime tracing")
        expanded, lists, resolved = _expand_filelists(argv, cwd, plan.adapter)
        roots.update(lists)
        complete &= resolved
        if not resolved:
            reasons.append("unresolved or unsupported filelist")
        for i, (arg, base) in enumerate(expanded):
            if arg == "-I" and i + 1 < len(expanded):
                incdirs.append(os.path.realpath(os.path.join(base, expanded[i + 1][0])))
            elif arg.startswith("-I") and len(arg) > 2:
                incdirs.append(os.path.realpath(os.path.join(base, arg[2:])))
            elif arg.startswith("+incdir+"):
                incdirs.extend(os.path.realpath(os.path.join(base, d))
                               for d in arg[len("+incdir+"):].split("+") if d)
            elif arg in ("-y", "--libdir") and i + 1 < len(expanded):
                directories.append(os.path.realpath(os.path.join(base, expanded[i + 1][0])))
            elif arg == "-CFLAGS" and i + 1 < len(expanded):
                flags = shlex.split(expanded[i + 1][0])
                for j, flag in enumerate(flags):
                    if flag.startswith("-I"):
                        directory = flag[2:] or (flags[j + 1] if j + 1 < len(flags) else "")
                        incdirs.append(os.path.realpath(os.path.join(base, directory)))
            path = os.path.realpath(os.path.join(base, arg))
            if path not in outputs and Path(path).is_file():
                roots.add(path)
    inventory, resolved = _directory_inputs(directories, outputs)
    roots.update(inventory)
    complete &= resolved
    if plan.adapter == "cocotb":
        complete = False
        reasons.append("Python packages and dynamic imports require runtime dependency tracing")
    files, resolved = dependency_files(roots, incdirs, workdirs, outputs)
    if not resolved:
        reasons.append("missing or dynamic include/runtime input")
    opaque, ok = _directory_inputs(plan.meta.get("toolchain_dirs", []), outputs)
    opaque.update(plan.meta.get("toolchain_files", []))
    for path in opaque:
        try:
            files[os.path.realpath(path)] = sha256_file(path)
        except OSError:
            files[os.path.realpath(path)] = "MISSING"
            ok = False
    complete &= ok
    return {"files": files, "complete": complete and resolved,
            "roots": sorted(roots), "incdirs": incdirs, "workdirs": workdirs,
            "outputs": sorted(outputs), "reasons": reasons,
            "request": {"workdir": plan.workdir, "adapter": plan.adapter,
                        "artifacts": plan.artifacts, "meta": {
                            k: plan.meta[k] for k in ("input_files", "input_dirs", "depfiles",
                                                     "toolchain_dirs", "toolchain_files")
                            if k in plan.meta}, "input_files": list(input_files),
                        "steps": [{"argv": s.argv, "cwd": s.cwd} for s in plan.steps]}}


def _expand_filelists(argv, cwd, adapter, stack=()):
    """Match native -f (cwd) and Verilator -F (file-relative) semantics.

    Unlike workspace source manifests these lists are compiler arguments.
    Unknown dialects, environment substitutions and cycles disable reuse.
    """
    expanded, files, complete = [], set(), True
    it = iter(argv)
    for arg in it:
        flag = arg if arg in ("-f", "-F", "-c") else ""
        joined = adapter == "icarus" and arg[:2] in ("-f", "-c") and len(arg) > 2
        if flag == "-c" and adapter != "icarus":
            flag = ""
        if not flag and not joined:
            expanded.append((arg, cwd))
            continue
        name = arg[2:] if joined else next(it, "")
        path = os.path.realpath(os.path.join(cwd, name))
        files.add(path)
        if (not name or path in stack or len(stack) > 64
                or adapter not in ("icarus", "verilator")
                or (flag == "-F" and adapter != "verilator")):
            complete = False
            continue
        try:
            text = Path(path).read_text()
            text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                          lambda m: m[0] if m[0].startswith('"') else " ", text, flags=re.S)
            # Native compilers expand variables using their own environment;
            # do not substitute the parent process's potentially different env.
            if "$" in text:
                complete = False
            args = shlex.split(text, comments=True)
            base = str(Path(path).parent) if flag == "-F" else cwd
            tokens, nested, ok = _expand_filelists(args, base, adapter, (*stack, path))
            expanded.extend(tokens)
            files.update(nested)
            complete &= ok
        except (OSError, ValueError):
            complete = False
    return expanded, files, complete


def _directory_inputs(directories, outputs):
    files, complete = set(), True
    for directory in directories:
        path = Path(directory)
        if not path.is_dir():
            files.add(str(path))
            complete = False
            continue
        errors = []
        for base, dirs, names in os.walk(path, onerror=errors.append):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
            if any(Path(base, d).is_symlink() for d in dirs):
                complete = False
            for name in sorted(names):
                f = os.path.realpath(os.path.join(base, name))
                if f not in outputs and not name.endswith((".pyc", ".pyo")):
                    files.add(f)
        complete &= not errors
    return files, complete


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
        if re.search(r'\$system\s*\(|import\s+"DPI-C"', text):
            complete = False
        if Path(path).suffix.lower() in {".c", ".cc", ".cpp", ".h", ".hpp"} and re.search(
                r'\b(?:ifstream|fstream|fopen|open|dlopen|system)\s*[(<{]', text):
            complete = False  # native runtime I/O is not a compiler dependency
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
        for m in re.finditer(r'\$fopen\s*\(\s*([^,)]+)(?:,\s*([^)]*))?\)', text):
            mode = m[2].strip() if m[2] else '"w"'
            if re.fullmatch(r'"[wa]b?"', mode):
                continue
            name = re.fullmatch(r'"([^"\n]+)"\s*', m[1])
            if not name or not re.fullmatch(r'"[rwa][b+]*"', mode):
                complete = False
                continue
            found = [os.path.realpath(os.path.join(d, name[1])) for d in workdirs
                     if Path(d, name[1]).is_file()]
            if not found:
                complete = False
            pending.extend(found)
    return files, complete


def snapshot_current(snapshot: dict) -> bool:
    if snapshot.get("unstable"):
        return False
    request = snapshot.get("request")
    if request:
        from types import SimpleNamespace
        plan = SimpleNamespace(**{k: v for k, v in request.items() if k != "input_files"})
        plan.steps = [SimpleNamespace(**s) for s in request["steps"]]
        files = capture_inputs(plan, request["input_files"])["files"]
        observed, _ = dependency_files(snapshot.get("observed", []),
                                        snapshot.get("incdirs", []), snapshot.get("workdirs", []),
                                        snapshot.get("outputs", []))
        files.update(observed)
    else:
        files, _ = dependency_files(snapshot.get("roots", []), snapshot.get("incdirs", []),
                                    snapshot.get("workdirs", []), snapshot.get("outputs", []))
    return files == snapshot.get("files", {})


def finish_capture(plan, before, input_files=()):
    """Attach compiler-observed inputs without claiming dynamic inputs are complete."""
    after = capture_inputs(plan, input_files)
    before["unstable"] = before["files"] != after["files"]
    observed = set()
    for dep in plan.meta.get("depfiles", []):
        try:
            text = Path(dep["path"]).read_text()
            if dep["format"] == "make":
                text = text.replace("\\\n", " ")
                names = [name for line in text.splitlines() if ":" in line
                         for name in shlex.split(line.split(":", 1)[1])]
            else:
                names = text.splitlines()
            observed.update(os.path.realpath(os.path.join(plan.workdir, n)) for n in names if n)
        except (OSError, ValueError):
            before["complete"] = False
            before["reasons"].append("native dependency output is missing or unreadable")
            continue
    # Generated files cannot be dependencies of the run that produced them.
    observed.difference_update(before["outputs"])
    observed = {p for p in observed if not any(
        Path(p).is_relative_to(d) for d in plan.meta.get("output_dirs", []))}
    native, _ = dependency_files(observed, before["incdirs"], before["workdirs"], before["outputs"])
    unknown = set(native) - set(before["files"])
    before["observed"] = sorted(observed)
    before["files"].update(native)
    if unknown:
        before["complete"] = False
        before["reasons"].append("compiler discovered inputs absent from the pre-run manifest")
    if before["unstable"]:
        before["complete"] = False
        before["reasons"].append("inputs changed while the job was running")
    return before
