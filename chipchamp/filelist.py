"""Filelist (`.f`) ingestion (SPEC §12.2, FR-PROJ-01).

Meets the flow: nested ``-f`` includes, ``+incdir+``, ``+define+``, and plain
source paths, resolved relative to each filelist's own directory. This is the
lingua franca of RTL projects; ingesting it unmodified is what lets Chipchamp be
useful on day one without a migration.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field


@dataclass
class FileSet:
    sources: list[str] = field(default_factory=list)
    incdirs: list[str] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)

    def dedup(self) -> "FileSet":
        seen = set()
        srcs = []
        for s in self.sources:
            a = os.path.abspath(s)
            if a not in seen:
                seen.add(a)
                srcs.append(s)
        self.sources = srcs
        self.incdirs = list(dict.fromkeys(self.incdirs))
        return self


def parse_filelist(path: str, _seen: set | None = None) -> FileSet:
    _seen = _seen or set()
    fs = FileSet()
    ap = os.path.abspath(path)
    if ap in _seen or not os.path.exists(path):
        return fs
    _seen.add(ap)
    base = os.path.dirname(path)
    with open(path, "r", errors="replace") as fh:
        raw = fh.read()
    # strip comments (// and #) and join line continuations
    raw = re.sub(r"//[^\n]*", "", raw)
    raw = re.sub(r"(?m)^\s*#[^\n]*", "", raw)
    tokens = raw.split()
    i = 0
    while i < len(tokens):
        tok = os.path.expandvars(tokens[i])
        if tok in ("-f", "-F", "-file"):
            i += 1
            if i < len(tokens):
                sub = _resolve(base, os.path.expandvars(tokens[i]))
                child = parse_filelist(sub, _seen)
                fs.sources += child.sources
                fs.incdirs += child.incdirs
                fs.defines.update(child.defines)
        elif tok.startswith("+incdir+"):
            for d in tok[len("+incdir+"):].split("+"):
                if d:
                    fs.incdirs.append(_resolve(base, d))
        elif tok.startswith("-I"):
            d = tok[2:] or (tokens[i + 1] if i + 1 < len(tokens) else "")
            if tok == "-I":
                i += 1
            if d:
                fs.incdirs.append(_resolve(base, d))
        elif tok.startswith("+define+"):
            for d in tok[len("+define+"):].split("+"):
                if "=" in d:
                    k, v = d.split("=", 1)
                    fs.defines[k] = v
                elif d:
                    fs.defines[d] = "1"
        elif tok.startswith("-D"):
            d = tok[2:]
            if "=" in d:
                k, v = d.split("=", 1)
                fs.defines[k] = v
            elif d:
                fs.defines[d] = "1"
        elif tok.startswith("-y") or tok in ("-v",):
            i += 1  # library dir/file — skip target, keep simple
        elif tok.startswith("-") or tok.startswith("+"):
            pass  # unknown switch: ignore
        else:
            # source path (maybe a glob)
            resolved = _resolve(base, tok)
            matches = glob.glob(resolved)
            fs.sources += matches if matches else [resolved]
        i += 1
    return fs.dedup()


def parse_fusesoc_core(path: str) -> FileSet:
    """FuseSoC CAPI2 ``.core`` ingestion (SPEC §12.2 FR-PROJ-01): collect RTL
    files + include dirs from all filesets (target resolution is FuseSoC's job;
    for indexing/sim we take the union of rtl-ish filesets)."""
    import yaml
    fs = FileSet()
    if not os.path.exists(path):
        return fs
    text = open(path, errors="replace").read()
    # CAPI2 files start with a "CAPI=2:" line that is not YAML
    text = re.sub(r"^CAPI\s*=\s*2\s*:\s*\n", "", text)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return fs
    base = os.path.dirname(path)
    for fset in (data.get("filesets") or {}).values():
        if not isinstance(fset, dict):
            continue
        for item in fset.get("files") or []:
            if isinstance(item, str):
                fname, attrs = item, {}
            elif isinstance(item, dict):
                fname, attrs = next(iter(item.items()))
                attrs = attrs or {}
            else:
                continue
            resolved = _resolve(base, fname)
            if attrs.get("is_include_file"):
                fs.incdirs.append(os.path.dirname(resolved))
            else:
                fs.sources.append(resolved)
    return fs.dedup()


def parse_bender_manifest(path: str) -> FileSet:
    """Bender.yml ingestion (SPEC §12.2): sources list (plain paths or
    {files: [...], include_dirs: [...], defines: {...}} groups)."""
    import yaml
    fs = FileSet()
    if not os.path.exists(path):
        return fs
    try:
        data = yaml.safe_load(open(path, errors="replace")) or {}
    except yaml.YAMLError:
        return fs
    base = os.path.dirname(path)

    def add_entry(entry):
        if isinstance(entry, str):
            fs.sources.append(_resolve(base, entry))
        elif isinstance(entry, dict):
            for d in entry.get("include_dirs") or []:
                fs.incdirs.append(_resolve(base, d))
            for k, v in (entry.get("defines") or {}).items():
                fs.defines[k] = "1" if v is None else str(v)
            for f in entry.get("files") or []:
                add_entry(f)

    for entry in data.get("sources") or []:
        add_entry(entry)
    return fs.dedup()


def resolve_sources(root: str, *, filelists=None, globs=None, sources=None,
                    fusesoc_core=None, bender=None) -> FileSet:
    fs = FileSet()
    for fl in filelists or []:
        child = parse_filelist(os.path.join(root, fl))
        fs.sources += child.sources
        fs.incdirs += child.incdirs
        fs.defines.update(child.defines)
    if fusesoc_core:
        child = parse_fusesoc_core(os.path.join(root, fusesoc_core))
        fs.sources += child.sources
        fs.incdirs += child.incdirs
    if bender:
        child = parse_bender_manifest(os.path.join(root, bender))
        fs.sources += child.sources
        fs.incdirs += child.incdirs
        fs.defines.update(child.defines)
    for g in globs or []:
        fs.sources += sorted(glob.glob(os.path.join(root, g)))
    for s in sources or []:
        fs.sources.append(os.path.join(root, s))
    return fs.dedup()


def _resolve(base: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))
