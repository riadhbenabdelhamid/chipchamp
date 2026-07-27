"""IP component library (reuse with receipts): manifest-driven discovery of
verified building blocks the agent can search, inspect and fetch instead of
rewriting common RTL from scratch.

A library root is a directory carrying a ``manifest.json`` with a ``modules``
list (the riscvier convention — see its ``docs/METADATA_SCHEMA.md``): each
entry names a component with category, tags, verification status, parameters,
clocks/resets and ports — enough to pick a block, parameterize it and
instantiate it without reading the RTL first.

Discovery is layered like skills, first-occurrence-wins:

1. ``[library] dirs = [...]`` in config.toml — versioned team intent
2. the CLI-managed registry (``.chipchamp/library.json``) — ad-hoc pointers
   added with ``chipchamp library add <dir>``

Fetching copies a component's RTL fileset (parsed from its FuseSoC ``.core``
when present, else by convention) into the workspace through the same policy
gate as any other write — fetched files are recorded edits, visible to gating.
Library metadata is data, not authority: nothing here executes library content.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .util.jsonio import atomic_write, dump_json, load_json

INDEX_MODES = ("compact", "full", "off")
_INDEX_CAP = 4_000  # chars; the library index must never flood the prompt


@dataclass
class LibModule:
    name: str
    meta: dict          # the raw manifest entry (params/ports/clocks/resets…)
    dir: Path           # the component directory (rtl/, tb/, <name>.core …)
    lib: str            # owning library name (manifest "library")
    root: Path          # library root (the manifest.json directory)
    source: str         # config | registry

    @property
    def category(self) -> str:
        return str(self.meta.get("category", ""))

    @property
    def summary(self) -> str:
        return str(self.meta.get("summary", ""))


@dataclass
class LibraryReport:
    libraries: list[dict] = field(default_factory=list)  # name/root/count/source
    modules: dict[str, LibModule] = field(default_factory=dict)
    shadowed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- registry (.chipchamp/library.json) ---------------------------------------


def registry_path(ws) -> Path:
    return Path(ws.dot) / "library.json"


def load_registry(ws) -> list[str]:
    p = registry_path(ws)
    if not p.exists():
        return []
    try:
        data = load_json(p)
    except Exception:
        return []
    dirs = data.get("dirs", []) if isinstance(data, dict) else []
    return [d for d in dirs if isinstance(d, str)]


def _save_registry(ws, dirs: list[str]) -> None:
    atomic_write(registry_path(ws), dump_json({"dirs": dirs}))


def add_registry_dir(ws, path: str) -> list[str]:
    p = str(Path(path).expanduser().resolve())
    dirs = load_registry(ws)
    if p not in dirs:
        dirs.append(p)
        _save_registry(ws, dirs)
    return dirs


def remove_registry_dir(ws, path: str | None = None, all: bool = False) -> list[str]:
    if all:
        _save_registry(ws, [])
        return []
    dirs = load_registry(ws)
    if path:
        p = str(Path(path).expanduser().resolve())
        dirs = [d for d in dirs if d != p]
        _save_registry(ws, dirs)
    return dirs


# ---- discovery ----------------------------------------------------------------


def library_dirs(ws) -> list[tuple[Path, str]]:
    """Ordered, deduped (root, source) pairs carrying a manifest.json."""
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(p: Path, source: str) -> None:
        try:
            key = str(p.expanduser().resolve())
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        if (Path(key) / "manifest.json").is_file():
            out.append((Path(key), source))

    cfg = (ws.config.get("library", {}) or {}) if hasattr(ws, "config") else {}
    for d in cfg.get("dirs", []) or []:
        p = Path(str(d)).expanduser()
        if not p.is_absolute():
            p = Path(ws.root) / p
        add(p, "config")
    for d in load_registry(ws):
        add(Path(d), "registry")
    return out


def _module_dir(root: Path, name: str, category: str) -> Path | None:
    """riscvier layout: <root>/<category>/<name> (full category path, e.g.
    bus/axi/axi_cdc) or <root>/<top-level-category>/<name> (storage/fifo_sync);
    tolerate flat and one-off nestings via a bounded glob."""
    cands = []
    if category:
        cands.append(root / category / name)
        cands.append(root / category.split("/")[0] / name)
    cands.append(root / name)
    for c in cands:
        if c.is_dir():
            return c
    for pat in (f"*/{name}", f"*/*/{name}"):
        hits = [p for p in root.glob(pat) if p.is_dir()]
        if hits:
            return hits[0]
    return None


def discover_report(ws) -> LibraryReport:
    """Scan every layer; first occurrence of a module name wins."""
    rep = LibraryReport()
    for root, source in library_dirs(ws):
        try:
            man = json.loads((root / "manifest.json").read_text(errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            rep.warnings.append(f"{root}/manifest.json: unreadable ({e}) — skipped")
            continue
        mods = man.get("modules")
        if not isinstance(mods, list):
            rep.warnings.append(f"{root}/manifest.json: no 'modules' list — skipped")
            continue
        lib = str(man.get("library", root.name))
        count = 0
        for entry in mods:
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                rep.warnings.append(f"{root}: manifest entry without a name — skipped")
                continue
            name = str(entry["name"]).strip()
            d = _module_dir(root, name, str(entry.get("category", "")))
            if d is None:
                rep.warnings.append(f"{lib}: '{name}' has no directory under {root}")
                continue
            if name in rep.modules:
                rep.shadowed.append(f"{name} ({root}) shadowed by "
                                    f"({rep.modules[name].root})")
                continue
            rep.modules[name] = LibModule(name=name, meta=entry, dir=d,
                                          lib=lib, root=root, source=source)
            count += 1
        rep.libraries.append({"name": lib, "root": str(root), "count": count,
                              "source": source})
    return rep


def discover(ws) -> dict[str, LibModule]:
    return discover_report(ws).modules


# ---- search -------------------------------------------------------------------


def search(ws, query: str = "", category: str = "", tag: str = "",
           limit: int = 12) -> list[LibModule]:
    """Ranked match over name/tags/category/summary/description."""
    mods = discover(ws)
    q = (query or "").lower().strip()
    words = [w for w in q.replace(",", " ").split() if w]
    out: list[tuple[int, LibModule]] = []
    for m in mods.values():
        meta = m.meta
        tags = [str(t).lower() for t in meta.get("tags", []) or []]
        if category and not m.category.lower().startswith(category.lower()):
            continue
        if tag and tag.lower() not in tags:
            continue
        hay = " ".join([m.name.lower(), m.category.lower(),
                        m.summary.lower(),
                        str(meta.get("description", "")).lower(),
                        " ".join(tags)])
        score = 0
        if q:
            if q == m.name.lower():
                score += 100
            elif q in m.name.lower():
                score += 60
            for w in words:
                if w in tags:
                    score += 30
                if w in m.name.lower():
                    score += 20
                if w in m.category.lower():
                    score += 15
                if w in hay:
                    score += 8
            if score == 0:
                continue
        out.append((score, m))
    out.sort(key=lambda t: (-t[0], t[1].name))
    return [m for _, m in out[:max(1, limit)]]


# ---- files + instantiation ------------------------------------------------------


def files_for(mod: LibModule) -> list[dict]:
    """The component's synthesizable fileset as
    ``{src, rel, include}`` records (src absolute; rel = suggested
    workspace-relative name). Prefers the FuseSoC ``.core`` 'rtl' fileset;
    falls back to the layout convention (common includes + rtl/*.sv)."""
    core = mod.dir / f"{mod.name}.core"
    files: list[tuple[Path, bool]] = []
    if core.is_file():
        try:
            import yaml
            data = yaml.safe_load(core.read_text(errors="replace")) or {}
            fs = (data.get("filesets", {}) or {}).get("rtl", {}) or {}
            for item in fs.get("files", []) or []:
                if isinstance(item, dict):
                    (path, attrs), = item.items()
                    inc = bool((attrs or {}).get("is_include_file"))
                else:
                    path, inc = str(item), False
                p = (mod.dir / str(path)).resolve()
                if p.is_file():
                    files.append((p, inc))
        except Exception:
            files = []
    if not files:
        for name in ("registers.svh", "assertions.svh"):
            p = mod.root / "common" / "rtl" / name
            if p.is_file():
                files.append((p, True))
        pkg = mod.root / "common" / "rtl" / f"{mod.lib}_pkg.sv"
        if pkg.is_file():
            files.append((pkg, False))
        rtl = mod.dir / "rtl"
        if rtl.is_dir():
            for p in sorted(rtl.glob("*.sv*")):
                files.append((p.resolve(), p.suffix == ".svh"))
    return [{"src": str(p), "rel": p.name, "include": inc} for p, inc in files]


def verification_collateral(mod: LibModule) -> dict:
    """The component's shipped verification views (SV TB, Verilator C++ harness
    + golden ref model, cocotb, UVM, formal), resolved against its directory —
    each with the absolute source path and whether it exists on disk. Drives
    system-level sim REUSE: the ``ref_model`` is a composable executable spec."""
    verif = mod.meta.get("verification") or {}
    out: dict = {}
    for key in ("sv_tb", "verilator_tb", "ref_model", "cocotb", "uvm", "formal"):
        rel = verif.get(key)
        if isinstance(rel, str) and rel:
            src = (mod.dir / rel)
            out[key] = {"path": rel, "src": str(src), "exists": src.is_file()}
    if verif.get("coverage"):
        out["coverage"] = verif["coverage"]
    return out


def sim_ref_files(mod: LibModule) -> list[dict]:
    """The C++ golden-model collateral to fetch for a system-level Verilator
    sim: the component's self-contained ``ref_<name>.hpp`` plus the shared
    harness base (``common/verif/verilator/tb_base.hpp``). Returns
    ``{src, rel}`` records (rel = suggested basename)."""
    files: list[dict] = []
    ref = (mod.meta.get("verification") or {}).get("ref_model")
    if isinstance(ref, str) and ref and (mod.dir / ref).is_file():
        files.append({"src": str(mod.dir / ref), "rel": Path(ref).name})
    base = mod.root / "common" / "verif" / "verilator" / "tb_base.hpp"
    if base.is_file():
        files.append({"src": str(base), "rel": "tb_base.hpp"})
    return files


def instantiation(mod: LibModule) -> str:
    """A ready-to-edit SV instantiation template from the manifest metadata."""
    meta = mod.meta
    lines = [f"{meta.get('top', mod.name)} #("]
    params = meta.get("params", []) or []
    for i, p in enumerate(params):
        comma = "," if i < len(params) - 1 else ""
        d = p.get("default", "")
        desc = str(p.get("desc", "")).strip()
        lines.append(f"  .{p.get('name')}({d}){comma}"
                     + (f"  // {desc}" if desc else ""))
    lines.append(f") u_{mod.name} (")
    ports = ([{"name": c.get("name"), "dir": "in", "width": 1}
              for c in meta.get("clocks", []) or []]
             + [{"name": r.get("name"), "dir": "in", "width": 1}
                for r in meta.get("resets", []) or []]
             + list(meta.get("ports", []) or []))
    for i, p in enumerate(ports):
        comma = "," if i < len(ports) - 1 else ""
        w = p.get("width", 1)
        note = f"{p.get('dir', '?'):3}" + (f" [{w}]" if str(w) not in ("1",) else "")
        lines.append(f"  .{p.get('name')}(){comma}  // {note}")
    lines.append(");")
    return "\n".join(lines)


def module_card(mod: LibModule) -> dict:
    """Full metadata card + fileset + instantiation template."""
    return {"name": mod.name, "library": mod.lib, "category": mod.category,
            "status": mod.meta.get("status", ""),
            "version": mod.meta.get("version", ""),
            "summary": mod.summary,
            "description": mod.meta.get("description", ""),
            "tags": mod.meta.get("tags", []),
            "params": mod.meta.get("params", []),
            "clocks": mod.meta.get("clocks", []),
            "resets": mod.meta.get("resets", []),
            "ports": mod.meta.get("ports", []),
            "dir": str(mod.dir),
            "files": files_for(mod),
            "verification": verification_collateral(mod),
            "instantiation": instantiation(mod)}


# ---- index (system prompt) ------------------------------------------------------


def library_index(ws, mode: str = "compact") -> str:
    """The system-prompt index: per library, its size and category map —
    compact enough to always ride along, rich enough to trigger lib.search."""
    if mode == "off":
        return ""
    rep = discover_report(ws)
    if not rep.modules:
        return ""
    lines = []
    for lib in rep.libraries:
        mods = [m for m in rep.modules.values()
                if m.lib == lib["name"] and str(m.root) == lib["root"]]
        cats: dict[str, int] = {}
        for m in mods:
            top = m.category.split("/")[0] or "misc"
            cats[top] = cats.get(top, 0) + 1
        catstr = ", ".join(f"{c}({n})" for c, n in sorted(cats.items()))
        lines.append(f"- {lib['name']}: {lib['count']} components — {catstr}")
        if mode == "full":
            for m in sorted(mods, key=lambda x: (x.category, x.name)):
                lines.append(f"    {m.name} [{m.category}] {m.summary}")
    text = "\n".join(lines)
    if len(text) > _INDEX_CAP:
        text = text[:_INDEX_CAP] + "\n…[index truncated — lib.search has the rest]"
    return text
