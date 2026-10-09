"""Bounded copies for mutation experiments; never edit the user's working tree."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import time

from ..tools.paths import PathRefused, ws_path
from ..util.hashing import sha256_file


def snapshot(ctx, destination: Path, deadline: float) -> dict:
    """Copy readable regular files, retaining an input manifest for the report.

    Runtime stores/build outputs and environment directories are omitted. Symlinks
    are omitted rather than followed into the live tree; required missing inputs
    subsequently fail validation or the mandatory baseline. This is isolation of
    source edits, not a sandbox for untrusted test programs.
    """
    from ..brand import dot_dir_names
    ignored = {".git", ".hg", ".svn", ".venv", "venv", "__pycache__",
               ".pytest_cache", ".mypy_cache", "node_modules", "obj_dir"}
    state = set(dot_dir_names())
    root = Path(ctx.ws.root)
    omitted, files, total = [], {}, 0
    destination.mkdir(parents=True)
    for directory, dirs, names in os.walk(root, followlinks=False):
        if time.monotonic() >= deadline:
            raise TimeoutError("mutation runtime limit reached while copying inputs")
        rel_dir = Path(directory).relative_to(root)
        dirs[:] = sorted(d for d in dirs if d not in ignored and
                         not d.startswith("obj_cpp_") and not d.startswith("obj_cov"))
        if rel_dir.parts and rel_dir.parts[0] in state:
            dirs[:] = []
            names = [n for n in names if n in ("config.toml", "config.local.toml", "tests.yaml", "policy.yaml")]
        for name in list(dirs) + sorted(names):
            source = Path(directory) / name
            relative = source.relative_to(root).as_posix()
            if source.is_symlink():
                omitted.append({"path": relative, "reason": "symlink omitted"})
                if name in dirs:
                    dirs.remove(name)
                continue
            if source.is_dir():
                continue
            if source.suffix in (".vcd", ".fst", ".vvp", ".pyc", ".o"):
                continue
            try:
                ws_path(ctx, relative)
            except PathRefused:
                omitted.append({"path": relative, "reason": "read denied by workspace policy"})
                continue
            if not stat.S_ISREG(source.stat().st_mode):
                omitted.append({"path": relative, "reason": "not a regular file"})
                continue
            size = source.stat().st_size
            total += size
            if size > 64 * 1024**2 or total > 256 * 1024**2 or len(files) >= 10000:
                raise ValueError("mutation snapshot exceeds 64 MiB/file, 256 MiB total or 10000 files; use a smaller workspace")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = sha256_file(source)
            shutil.copy2(source, target)
            if before != sha256_file(target) or before != sha256_file(source):
                raise ValueError(f"input changed while copying: {relative}")
            files[relative] = before
    # Catch edits to files copied early while later files were still copying.
    if any(sha256_file(root / p) != digest for p, digest in files.items()):
        raise ValueError("workspace changed while preparing mutation inputs; retry")
    return {"files": files, "bytes": total, "omitted": omitted}


def validate_inputs(ctx, files):
    """Absolute references to the original tree must never bypass the mutant."""
    from ..jobs.inputs import dependency_files
    root = Path(ctx.ws.root).resolve()
    roots = [*files, *ctx.target.fileset.manifests]
    for path in [*roots, *ctx.target.incdirs]:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("mutation experiments require workspace-local source files, filelists and include directories")
        if not resolved.exists():
            raise ValueError(f"input absent from mutation snapshot: {resolved.relative_to(root)}")
    dependencies, _ = dependency_files(roots, ctx.target.incdirs, [str(root)])
    if any(not Path(p).resolve().is_relative_to(root) for p in dependencies):
        raise ValueError("mutation experiments require workspace-local HDL dependencies")
