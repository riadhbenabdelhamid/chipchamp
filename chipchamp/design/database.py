"""The design database (SPEC §8.3): a persistent, queryable semantic index.

Ingests the project's SystemVerilog, builds an elaboration-accurate hierarchy
with resolved parameters, cross-references, driver/load sets, clock/reset
domains and FSMs, and answers the ``design.*`` agent tools. Persisted to
``.chipchamp/db.json`` with the tree revision + per-file hashes it reflects, so a
tool answering from a stale index can say so (FR-DB-06).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..util.hashing import sha256_file, sha256_text
from ..util.jsonio import atomic_write, dump_json, load_json
from . import slang_frontend
from .cone import ConeSlice, extract_cone
from .domains import DomainMap, infer_domains
from .fsm import extract_fsms
from .model import FSM, HierNode, Module, Port
from .params import eval_const
from .parser import parse_source


@dataclass
class SourceFile:
    path: str
    sha256: str
    lang: str = "sv"
    modules: list[str] = field(default_factory=list)


@dataclass
class ModuleCard:
    """~200-token summary the context assembler pins (SPEC §8.9 L2)."""
    name: str
    file: str
    line: int
    summary: str
    ports: list[dict]
    params: list[dict]
    clocks: list[str]
    resets: list[str]
    instances: list[str]
    has_fsm: bool
    confidence: str


class DesignDB:
    def __init__(self, root: str = "."):
        self.root = Path(root)
        self.modules: dict[str, Module] = {}
        self.files: dict[str, SourceFile] = {}
        self.domains: dict[str, DomainMap] = {}
        self.fsms: dict[str, list[FSM]] = {}
        self.domain_overrides: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []
        self.built_at: float = 0.0
        self.tree_rev: str = ""
        self.frontend: str = "pragmatic"  # "slang" once a slang build succeeds
        self._sv_specs: list[tuple[str, dict, list]] = []  # (path, defines, incdirs)

    # ---- ingestion ----------------------------------------------------------

    def add_file(self, path: str, defines: Optional[dict] = None,
                 incdirs: Optional[list[str]] = None) -> None:
        """Record a source file. Actual parsing happens in :meth:`build` — with
        slang over the whole design when available (accurate, cross-file), else
        the pragmatic per-file parser."""
        p = Path(path)
        try:
            sha = sha256_file(p)
        except OSError as e:
            self.warnings.append(f"cannot read {path}: {e}")
            return
        lang = "vhdl" if p.suffix.lower() in (".vhd", ".vhdl") else "sv"
        self.files[str(p)] = SourceFile(path=str(p), sha256=sha, lang=lang)
        if lang == "vhdl":
            self.warnings.append(f"{path}: VHDL indexed at file level only")
        else:
            self._sv_specs.append((str(p), defines or {}, incdirs or []))

    def _parse_pragmatic(self) -> None:
        for path, defines, incdirs in self._sv_specs:
            try:
                text = Path(path).read_text(errors="replace")
            except OSError:
                continue
            for mod in parse_source(text, path, defines, incdirs, str(Path(path).parent)):
                if mod.name in self.modules:
                    self.warnings.append(f"duplicate module '{mod.name}' in {path}")
                self.modules[mod.name] = mod
                if path in self.files:
                    self.files[path].modules.append(mod.name)

    def _parse_frontend(self) -> None:
        """Prefer slang (production SV front end); fall back to the pragmatic
        parser on any failure so indexing never hard-fails (FR-DB-07)."""
        self.modules = {}
        sv_paths = [s[0] for s in self._sv_specs]
        if slang_frontend.available() and sv_paths:
            defines = {k: v for _, d, _ in self._sv_specs for k, v in d.items()}
            incdirs = sorted({i for _, _, ii in self._sv_specs for i in ii})
            try:
                mods = slang_frontend.compile_design(sv_paths, defines, incdirs)
                if mods:
                    self.modules = mods
                    self.frontend = "slang"
                    self._map_files_to_modules()
                    return
            except Exception as e:
                self.warnings.append(f"slang front end failed ({e}); "
                                     f"using the pragmatic parser")
        self.frontend = "pragmatic"
        self._parse_pragmatic()

    def _map_files_to_modules(self) -> None:
        for name, mod in self.modules.items():
            if mod.file and mod.file in self.files:
                self.files[mod.file].modules.append(name)

    def build(self, overrides: Optional[dict[str, dict[str, str]]] = None) -> None:
        if self._sv_specs:  # fresh index; load() populates modules without specs
            self._parse_frontend()
        self.domain_overrides = overrides or {}
        self.domains = {}
        self.fsms = {}
        for name, mod in self.modules.items():
            self.domains[name] = infer_domains(mod, self.domain_overrides.get(name))
            fs = extract_fsms(mod)
            if fs:
                self.fsms[name] = fs
        self.built_at = time.time()
        self.tree_rev = sha256_text("\n".join(
            f"{f.path}:{f.sha256}" for f in sorted(self.files.values(), key=lambda x: x.path)))

    # ---- staleness ----------------------------------------------------------

    def stale_files(self) -> list[str]:
        stale = []
        for path, sf in self.files.items():
            try:
                if sha256_file(path) != sf.sha256:
                    stale.append(path)
            except OSError:
                stale.append(path)
        return stale

    # ---- queries (back the design.* tools) ----------------------------------

    def module(self, name: str) -> Optional[Module]:
        return self.modules.get(name)

    def tops(self) -> list[str]:
        """Modules never instantiated by any other module."""
        instantiated = {i.module_type for m in self.modules.values() for i in m.instances}
        return sorted(n for n in self.modules if n not in instantiated)

    def module_card(self, name: str) -> Optional[ModuleCard]:
        m = self.modules.get(name)
        if not m:
            return None
        dm = self.domains.get(name)
        ins = sorted({p.name for p in m.ports if p.direction == "input"})
        outs = sorted({p.name for p in m.ports if p.direction == "output"})
        summary = (f"{name}: {len(m.ports)} ports "
                   f"({len(ins)} in / {len(outs)} out), {len(m.params)} params, "
                   f"{len(m.instances)} sub-instances, "
                   f"{len(m.always_blocks)} procedural blocks.")
        if dm and dm.clocks:
            summary += f" Clocks: {', '.join(sorted(dm.clocks))}."
        if name in self.fsms:
            summary += f" FSM with {len(self.fsms[name][0].states)} states."
        return ModuleCard(
            name=name, file=m.file, line=m.line, summary=summary,
            ports=[{"name": p.name, "dir": p.direction, "width": p.width_expr,
                    "type": p.dtype} for p in m.ports],
            params=[{"name": p.name, "default": p.default_expr} for p in m.params
                    if not p.is_localparam],
            clocks=sorted(dm.clocks) if dm else [],
            resets=sorted(dm.resets) if dm else [],
            instances=[f"{i.inst_name}:{i.module_type}" for i in m.instances],
            has_fsm=name in self.fsms, confidence=m.parse_confidence)

    def elaborate(self, top: str, params: Optional[dict[str, str]] = None,
                  max_depth: int = 64) -> HierNode:
        return self._elab(top, "", top, params or {}, max_depth, set())

    def _resolve_params(self, mod: Module, overrides: dict[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        ns: dict[str, object] = {}
        for p in mod.params:
            expr = overrides.get(p.name, p.default_expr)
            val = eval_const(expr, ns)
            resolved[p.name] = str(val) if val is not None else expr
            ns[p.name] = val if val is not None else expr
        return resolved

    def _elab(self, module: str, inst_name: str, path: str,
              overrides: dict[str, str], depth: int, stack: set[str]) -> HierNode:
        mod = self.modules.get(module)
        node = HierNode(inst_name=inst_name, module=module, path=path)
        if mod is None:
            node.unresolved = True
            node.note = "module definition not found in index"
            return node
        if module in stack or depth <= 0:
            node.note = "recursion/depth limit"
            return node
        resolved = self._resolve_params(mod, overrides)
        node.params = resolved
        for inst in mod.instances:
            child_over = {}
            for k, expr in inst.param_overrides.items():
                v = eval_const(expr, {kk: (int(vv) if vv.lstrip("-").isdigit() else vv)
                                      for kk, vv in resolved.items()})
                child_over[k] = str(v) if v is not None else expr
            child = self._elab(inst.module_type, inst.inst_name,
                               f"{path}.{inst.inst_name}", child_over,
                               depth - 1, stack | {module})
            node.children.append(child)
        return node

    def find_instances(self, module: str) -> list[str]:
        """Hierarchical paths where `module` is instantiated (from all tops)."""
        hits: list[str] = []
        for top in self.tops():
            self._collect_instances(self.elaborate(top), module, hits)
        return sorted(set(hits))

    def _collect_instances(self, node: HierNode, target: str, out: list[str]) -> None:
        if node.module == target and node.inst_name:
            out.append(node.path)
        for c in node.children:
            self._collect_instances(c, target, out)

    def xref(self, name: str) -> dict:
        result = {"definition": None, "instantiations": [], "ports": [], "params": []}
        if name in self.modules:
            m = self.modules[name]
            result["definition"] = f"{m.file}:{m.line}"
        for mod in self.modules.values():
            for inst in mod.instances:
                if inst.module_type == name:
                    result["instantiations"].append(
                        f"{inst.file}:{inst.line} ({mod.name}.{inst.inst_name})")
                if inst.inst_name == name:
                    result["instantiations"].append(f"{inst.file}:{inst.line}")
        return result

    def cone(self, module: str, signal: str, direction: str = "fanin",
             depth: int = 3) -> Optional[ConeSlice]:
        mod = self.modules.get(module)
        if not mod:
            return None
        dm = self.domains.get(module)
        domain_of = (lambda s: dm.signal_domain.get(s)) if dm else None

        def port_dir(mtype: str, port: str):
            child = self.modules.get(mtype)
            p = child.port(port) if child else None
            return p.direction if p else None

        return extract_cone(mod, signal, direction, depth, domain_of, port_dir=port_dir)

    def domain_report(self, module: str) -> Optional[DomainMap]:
        return self.domains.get(module)

    # ---- semantic diff (FR-DB-08) -------------------------------------------

    def semantic_diff(self, other: "DesignDB") -> dict:
        """Interface/hierarchy/domain deltas of `self` (new) vs `other` (old)."""
        diff = {"added_modules": [], "removed_modules": [], "port_changes": [],
                "param_changes": [], "domain_changes": [], "instance_changes": []}
        for name in self.modules.keys() - other.modules.keys():
            diff["added_modules"].append(name)
        for name in other.modules.keys() - self.modules.keys():
            diff["removed_modules"].append(name)
        for name in self.modules.keys() & other.modules.keys():
            a, b = self.modules[name], other.modules[name]
            ap = {p.name: p for p in a.ports}
            bp = {p.name: p for p in b.ports}
            for pn in ap.keys() ^ bp.keys():
                diff["port_changes"].append(
                    {"module": name, "port": pn,
                     "change": "added" if pn in ap else "removed"})
            for pn in ap.keys() & bp.keys():
                if (ap[pn].direction, ap[pn].width_expr) != (bp[pn].direction, bp[pn].width_expr):
                    diff["port_changes"].append(
                        {"module": name, "port": pn, "change": "modified",
                         "from": f"{bp[pn].direction} {bp[pn].width_expr}",
                         "to": f"{ap[pn].direction} {ap[pn].width_expr}"})
            aparm = {p.name: p.default_expr for p in a.params}
            bparm = {p.name: p.default_expr for p in b.params}
            for pn in set(aparm) | set(bparm):
                if aparm.get(pn) != bparm.get(pn):
                    diff["param_changes"].append(
                        {"module": name, "param": pn,
                         "from": bparm.get(pn), "to": aparm.get(pn)})
            ai = {i.inst_name: i.module_type for i in a.instances}
            bi = {i.inst_name: i.module_type for i in b.instances}
            for inm in set(ai) ^ set(bi):
                diff["instance_changes"].append(
                    {"module": name, "instance": inm,
                     "change": "added" if inm in ai else "removed"})
            da = self.domains.get(name)
            db = other.domains.get(name)
            if da and db and da.signal_domain != db.signal_domain:
                changed = [s for s in set(da.signal_domain) | set(db.signal_domain)
                           if da.signal_domain.get(s) != db.signal_domain.get(s)]
                if changed:
                    diff["domain_changes"].append({"module": name, "signals": sorted(changed)})
        return diff

    # ---- persistence --------------------------------------------------------

    def save(self, path: Optional[str] = None) -> str:
        path = path or str(self.root / ".chipchamp" / "db.json")
        payload = {
            "version": 1,
            "built_at": self.built_at,
            "tree_rev": self.tree_rev,
            "root": str(self.root),
            "warnings": self.warnings,
            "files": {p: {"sha256": f.sha256, "lang": f.lang, "modules": f.modules}
                      for p, f in self.files.items()},
            "modules": {n: _module_to_dict(m) for n, m in self.modules.items()},
            "domain_overrides": self.domain_overrides,
        }
        atomic_write(path, dump_json(payload))
        return path

    @classmethod
    def load(cls, path: str) -> "DesignDB":
        data = load_json(path)
        db = cls(root=data.get("root", "."))
        db.built_at = data.get("built_at", 0.0)
        db.tree_rev = data.get("tree_rev", "")
        db.warnings = data.get("warnings", [])
        for p, f in data.get("files", {}).items():
            db.files[p] = SourceFile(path=p, sha256=f["sha256"], lang=f.get("lang", "sv"),
                                     modules=f.get("modules", []))
        for n, md in data.get("modules", {}).items():
            db.modules[n] = _module_from_dict(md)
        db.build(data.get("domain_overrides", {}))
        return db


# ---- (de)serialization of the module model ---------------------------------


def _module_to_dict(m: Module) -> dict:
    from dataclasses import asdict
    d = asdict(m)
    # sets -> sorted lists for JSON
    for blk in d["always_blocks"]:
        blk["lhs"] = sorted(blk["lhs"])
        blk["rhs"] = sorted(blk["rhs"])
    for ca in d["assigns"]:
        ca["lhs_signals"] = sorted(ca["lhs_signals"])
        ca["rhs_signals"] = sorted(ca["rhs_signals"])
    return d


def _module_from_dict(d: dict) -> Module:
    from .model import (AlwaysBlock, ContinuousAssign, EnumDef, Instance, Net,
                        Parameter)
    m = Module(name=d["name"], file=d.get("file", ""), line=d.get("line", 0),
               line_end=d.get("line_end", 0), is_ansi=d.get("is_ansi", True),
               parse_confidence=d.get("parse_confidence", "high"))
    m.params = [Parameter(**p) for p in d.get("params", [])]
    m.ports = [Port(**p) for p in d.get("ports", [])]
    m.instances = [Instance(**i) for i in d.get("instances", [])]
    m.nets = [Net(**n) for n in d.get("nets", [])]
    m.enums = [EnumDef(**e) for e in d.get("enums", [])]
    for a in d.get("always_blocks", []):
        m.always_blocks.append(AlwaysBlock(
            kind=a["kind"], sens=[tuple(s) for s in a.get("sens", [])],
            clock=a.get("clock"), resets=a.get("resets", []),
            lhs=set(a.get("lhs", [])), rhs=set(a.get("rhs", [])),
            body=a.get("body", ""), file=a.get("file", ""), line=a.get("line", 0)))
    for c in d.get("assigns", []):
        m.assigns.append(ContinuousAssign(
            lhs=c["lhs"], rhs=c["rhs"], lhs_signals=set(c.get("lhs_signals", [])),
            rhs_signals=set(c.get("rhs_signals", [])), file=c.get("file", ""),
            line=c.get("line", 0)))
    return m
