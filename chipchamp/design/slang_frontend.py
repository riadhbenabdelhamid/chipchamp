"""Production SystemVerilog front-end via slang (pyslang).

The pragmatic regex parser (``parser.py``) is portable but silently fails on real
RTL — interface ports, ``import pkg::*`` headers, generate blocks — returning
empty/wrong facts while still reporting high confidence. slang is the industry
open-source SV front-end; this module compiles the whole design together and
extracts the same :mod:`chipchamp.design.model` objects, so every ``design.*``
tool, the cone/domain/FSM analyses and the hierarchy get elaboration-accurate
data for free. It is used when pyslang is installed and the design compiles;
otherwise the DB falls back to the pragmatic parser per file (FR-DB-07).

Design decisions:
- We walk slang's *elaborated* top instances (DFS) and record each module
  definition once. Ports/params carry slang's resolved values, so port widths and
  parameters are correct (``logic[3:0]``, ``WIDTH=4``) rather than raw expressions.
- Procedural internals (sensitivity list, assigned/read signals) are recovered by
  reusing the proven regex helpers on each block's exact source text from slang —
  accurate structure from slang, cheap internals from the existing extractor.
"""
from __future__ import annotations

import re
from typing import Optional

from .model import (AlwaysBlock, ContinuousAssign, EnumDef, Instance, Module,
                    Net, Parameter, Port)
from .parser import _lhs_rhs, _parse_sens, _refine_resets, signal_names


def available() -> bool:
    try:
        import pyslang  # noqa: F401
        return True
    except Exception:
        return False


def _get(obj, *names, call=True):
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if call and callable(v):
                try:
                    return v()
                except Exception:
                    return None
            return v
    return None


def _kind(sym) -> str:
    return str(getattr(sym, "kind", "")).split(".")[-1]


def _loc_line(sym, sm) -> int:
    try:
        loc = sym.location
        return sm.getLineNumber(loc)
    except Exception:
        return 0


def _range_of(t) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(msb, lsb, width_expr) from a slang type. The type stringifies with its
    resolved packed range, e.g. ``logic[7:0]`` — parse that (robust across the
    pyslang accessor churn)."""
    s = str(t) if t is not None else ""
    m = re.search(r"\[\s*([^\]:]+?)\s*:\s*([^\]]+?)\s*\]", s)
    if m:
        return m.group(1), m.group(2), f"[{m.group(1)}:{m.group(2)}]"
    return None, None, None


_DIR = {"In": "input", "Out": "output", "InOut": "inout", "Ref": "ref"}


class SlangError(Exception):
    pass


def compile_design(files: list[str], defines: Optional[dict] = None,
                   incdirs: Optional[list[str]] = None) -> dict[str, Module]:
    """Compile all SV files together and return {module_name: Module}.

    Raises SlangError on a hard failure so the caller can fall back."""
    import pyslang
    from pyslang.ast import Compilation
    from pyslang.syntax import SyntaxTree

    try:
        options = pyslang.Bag()
    except Exception:
        options = None
    comp = Compilation()
    sm = None
    added = 0
    for f in files:
        try:
            tree = SyntaxTree.fromFile(f) if hasattr(SyntaxTree, "fromFile") \
                else SyntaxTree.fromText(open(f, errors="replace").read(), f)
            # fromFile may return (tree, ...) or a tree
            if isinstance(tree, tuple):
                tree = tree[0]
            comp.addSyntaxTree(tree)
            if sm is None:
                sm = tree.sourceManager
            added += 1
        except Exception:
            continue
    if not added:
        raise SlangError("no files parsed by slang")

    modules: dict[str, Module] = {}
    seen: set[str] = set()
    root = comp.getRoot()
    enums = _collect_enums(comp, sm)

    def visit(inst):
        defn = _get(inst, "definition", call=False)
        name = getattr(defn, "name", None) or getattr(inst.body, "name", None)
        body = inst.body
        if name and name not in seen:
            seen.add(name)
            modules[name] = _module_from_body(name, body, sm, enums)
        for m in body:
            if _kind(m) == "Instance":
                visit(m)
            elif _kind(m) in ("GenerateBlock", "GenerateBlockArray"):
                _visit_generate(m, visit)

    for top in root.topInstances:
        visit(top)
    if not modules:
        raise SlangError("slang produced no modules")
    return modules


def _visit_generate(gen, visit):
    for m in _iter_scope(gen):
        k = _kind(m)
        if k == "Instance":
            visit(m)
        elif k in ("GenerateBlock", "GenerateBlockArray"):
            _visit_generate(m, visit)


def _iter_scope(scope):
    try:
        return list(scope)
    except Exception:
        members = _get(scope, "members")
        return list(members) if members else []


def _module_from_body(name: str, body, sm, enums: dict) -> Module:
    mod = Module(name=name, file=_file_of(body, sm), line=_loc_line(body, sm),
                 parse_confidence="high")
    mod.enums = list(enums.values())  # package/global enums visible here
    for m in body:
        k = _kind(m)
        if k == "TypeAlias":  # module-local typedef enum (the common FSM case)
            e = _enum_from_alias(m, sm)
            if e:
                mod.enums.append(e)
        elif k in ("Port", "InterfacePort"):
            mod.ports.append(_port(m, sm))
        elif k == "Parameter":
            mod.params.append(Parameter(
                name=m.name, default_expr=str(_get(m, "value", call=False) or ""),
                is_localparam=bool(_get(m, "isLocalParam", call=False)),
                file=mod.file, line=_loc_line(m, sm)))
        elif k == "Instance":
            mod.instances.append(_instance(m, sm))
        elif k == "ProceduralBlock":
            blk = _always(m, sm)
            if blk:
                mod.always_blocks.append(blk)
        elif k == "ContinuousAssign":
            ca = _assign(m, sm)
            if ca:
                mod.assigns.append(ca)
        elif k in ("Net", "Variable"):
            if not any(p.name == m.name for p in mod.ports):
                msb, lsb, _ = _range_of(_get(m, "type", call=False))
                mod.nets.append(Net(name=m.name, dtype=k.lower(),
                                    msb=msb, lsb=lsb, file=mod.file,
                                    line=_loc_line(m, sm)))
    return mod


def _port(sym, sm) -> Port:
    t = _get(sym, "type", call=False)
    msb, lsb, wexpr = _range_of(t)
    d = str(_get(sym, "direction", call=False) or "").split(".")[-1]
    return Port(name=sym.name, direction=_DIR.get(d, "input"),
                dtype=str(t) if t is not None else "logic",
                msb=msb, lsb=lsb, width_expr=wexpr,
                file=_file_of(sym, sm), line=_loc_line(sym, sm))


def _instance(sym, sm) -> Instance:
    defn = _get(sym, "definition", call=False)
    mtype = getattr(defn, "name", None) or getattr(sym.body, "name", "?")
    conns: dict[str, str] = {}
    # Prefer the instantiation's own source text — robust for all directions
    # (slang wraps output connections as Assignment exprs, losing the net name).
    syn = _get(sym, "syntax", call=False)
    if syn is not None:
        for mm in re.finditer(r"\.\s*(\w+)\s*\(([^()]*)\)", str(syn)):
            conns[mm.group(1)] = mm.group(2).strip()
    if not conns:
        for p in [p for p in sym.body if _kind(p) in ("Port", "InterfacePort")]:
            try:
                conns[p.name] = _conn_text(sym.getPortConnection(p))
            except Exception:
                pass
    overrides = {m.name: str(_get(m, "value", call=False))
                 for m in sym.body if _kind(m) == "Parameter"
                 and not _get(m, "isLocalParam", call=False)}
    return Instance(inst_name=sym.name, module_type=mtype, connections=conns,
                    param_overrides=overrides, file=_file_of(sym, sm),
                    line=_loc_line(sym, sm))


def _conn_text(conn) -> str:
    """The connected net expression for a port. slang wraps output connections as
    Assignment expressions (losing the net name), so prefer the connection's
    source syntax ``.port(net)`` and extract the inner expression from it."""
    if conn is None:
        return ""
    syn = _get(conn, "syntax", call=False)
    if syn is not None:
        s = str(syn)
        m = re.search(r"\(\s*(.*?)\s*\)\s*$", s, re.S)  # .port(expr)
        if m:
            return m.group(1).strip()
        m = re.match(r"\s*\.?\s*(\w+)\s*$", s)  # implicit .name
        if m:
            return m.group(1)
        return s.strip()
    expr = _get(conn, "expression", call=False)
    if expr is not None:
        es = _get(expr, "syntax", call=False)
        return str(es).strip() if es is not None else ""
    return ""


def _block_text(sym) -> str:
    syn = _get(sym, "syntax", call=False)
    return str(syn) if syn is not None else ""


def _always(sym, sm) -> Optional[AlwaysBlock]:
    pk = str(_get(sym, "procedureKind", call=False) or "")
    kind = {"AlwaysFF": "always_ff", "AlwaysComb": "always_comb",
            "AlwaysLatch": "always_latch", "Always": "always",
            "Initial": "initial", "Final": "final"}.get(pk.split(".")[-1])
    if kind in (None, "initial", "final"):
        return None
    text = _block_text(sym)
    m = re.search(r"@\s*\(([^)]*)\)", text)
    sens = _parse_sens_text(m.group(1)) if m else []
    clock = next((s for e, s in sens if e in ("posedge", "negedge")), None)
    resets = _refine_resets(text, [s for _, s in sens], clock)
    if resets and clock in resets:
        clock = next((s for e, s in sens if s not in resets), clock)
    lhs, rhs = _lhs_rhs(text)
    return AlwaysBlock(kind=kind, sens=sens, clock=clock, resets=resets,
                       lhs=lhs, rhs=rhs, body=text, file=_file_of(sym, sm),
                       line=_loc_line(sym, sm))


def _parse_sens_text(inner: str):
    sens = []
    for seg in re.split(r"\bor\b|,", inner):
        seg = seg.strip()
        if not seg or seg == "*":
            continue
        mm = re.match(r"(posedge|negedge)\s+([A-Za-z_]\w*)", seg)
        if mm:
            sens.append((mm.group(1), mm.group(2)))
        else:
            m2 = re.match(r"([A-Za-z_]\w*)", seg)
            if m2:
                sens.append(("level", m2.group(1)))
    return sens


def _assign(sym, sm) -> Optional[ContinuousAssign]:
    text = _block_text(sym)
    m = re.search(r"assign\s+(.+?)=(?!=)(.*)", text, re.S) or \
        re.match(r"\s*(.+?)=(?!=)(.*)", text, re.S)
    if not m:
        return None
    lhs, rhs = m.group(1).strip(), m.group(2).strip().rstrip(";")
    return ContinuousAssign(lhs=lhs, rhs=rhs, lhs_signals=signal_names(lhs),
                            rhs_signals=signal_names(rhs), file=_file_of(sym, sm),
                            line=_loc_line(sym, sm))


def _enum_from_alias(m, sm) -> Optional[EnumDef]:
    tt = _get(m, "targetType", call=False)
    # targetType is a DeclaredType wrapper; .type is the resolved EnumType, which
    # is iterable -> its enum value symbols (name + constant value).
    et = _get(tt, "type", call=False) if tt is not None and hasattr(tt, "type") else tt
    if et is None or not _get(et, "isEnum", call=False):
        return None
    mem = {}
    try:
        for e in et:
            mem[e.name] = str(_get(e, "value", call=False) or "")
    except Exception:
        return None
    return EnumDef(name=m.name, members=mem, line=_loc_line(m, sm)) if mem else None


def _collect_enums(comp, sm) -> dict[str, EnumDef]:
    """Package/global-scope enums (module-local ones are collected per module)."""
    enums: dict[str, EnumDef] = {}

    def scan(scope):
        for m in _iter_scope(scope):
            k = _kind(m)
            if k == "TypeAlias":
                e = _enum_from_alias(m, sm)
                if e:
                    enums[e.name] = e
            elif k in ("Package", "CompilationUnit"):
                scan(m)

    try:
        for cu in comp.getCompilationUnits():
            scan(cu)
    except Exception:
        pass
    try:
        for pkg in comp.getPackages():
            scan(pkg)
    except Exception:
        pass
    return enums


def _file_of(sym, sm) -> str:
    try:
        return sm.getFileName(sym.location)
    except Exception:
        return ""
