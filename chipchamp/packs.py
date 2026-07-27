"""Knowledge-pack discovery (SPEC §15.3).

Packs are directories of protocol/methodology collateral (templates, SVA
checkers, formal harnesses, pitfalls docs). Resolution order: the workspace's
own ``packs/`` (company packs) first, then the packs shipped with the Chipchamp
checkout — so a project can override a shipped pack by name.
"""
from __future__ import annotations

from pathlib import Path

_SHIPPED = Path(__file__).resolve().parent.parent / "packs"


def pack_dirs(ws_root: str | None = None) -> list[Path]:
    dirs = []
    if ws_root:
        local = Path(ws_root) / "packs"
        if local.is_dir():
            dirs.append(local)
    if _SHIPPED.is_dir():
        dirs.append(_SHIPPED)
    return dirs


def list_packs(ws_root: str | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for base in pack_dirs(ws_root):
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            if d.name in seen:
                continue
            seen.add(d.name)
            readme = d / "README.md"
            head = ""
            if readme.exists():
                for line in readme.read_text(errors="replace").splitlines():
                    if line.strip() and not line.startswith("#"):
                        head = line.strip()
                        break
            out.append({"name": d.name, "path": str(d),
                        "files": sorted(f.name for f in d.rglob("*") if f.is_file()),
                        "summary": head})
    return out


def pack_file(name: str, relpath: str, ws_root: str | None = None) -> Path | None:
    for base in pack_dirs(ws_root):
        cand = base / name / relpath
        if cand.exists():
            return cand
    return None
