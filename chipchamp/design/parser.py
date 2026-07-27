"""Pragmatic SystemVerilog parser producing :mod:`chipchamp.design.model` objects.

Token-driven, tolerant, and recovery-oriented: a construct it cannot understand
is skipped rather than aborting the file, and the affected module is flagged
``parse_confidence = "degraded"`` (FR-DB-07). It targets the synthesizable subset
plus the procedural patterns needed for domain inference and FSM extraction.
"""
from __future__ import annotations

import re
from typing import Optional

from .lexer import KEYWORDS, Token, preprocess, tokenize
from .model import (
    AlwaysBlock,
    ContinuousAssign,
    EnumDef,
    Instance,
    Module,
    Net,
    Parameter,
    Port,
)

_IDENT = re.compile(r"[A-Za-z_]\w*")
_NET_TYPES = {"logic", "wire", "reg", "bit", "var", "integer", "int", "byte",
              "shortint", "longint", "time", "genvar"}
_DIRECTIONS = {"input", "output", "inout"}
_NON_INSTANCE_HEADS = KEYWORDS | {"assert", "assume", "cover", "property",
                                  "endproperty", "sequence", "bind", "final",
                                  "not", "buf", "nand", "nor", "xor", "xnor",
                                  "pmos", "nmos", "tran", "supply0", "supply1"}


_BASED_NUM = re.compile(r"\d[\d_]*\s*'\s*[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+|'\s*[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+")
_SYSCALL = re.compile(r"\$\w+")


def signal_names(text: str) -> set[str]:
    """Base signal identifiers in an expression, excluding keywords, numbers,
    based literals (``1'b1``), system tasks and function-call heads."""
    text = _BASED_NUM.sub(" ", text)
    text = _SYSCALL.sub(" ", text)
    out: set[str] = set()
    called = set()
    # crude: NAME immediately followed by '(' is a call, not a signal
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", text):
        called.add(m.group(1))
    for name in _IDENT.findall(text):
        if name in KEYWORDS or name in called:
            continue
        out.add(name)
    return out


def _match(tokens: list[Token], i: int) -> int:
    """Given an opener token at index i, return index of the matching closer."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = tokens[i].value
    closer = pairs[opener]
    depth = 0
    while i < len(tokens):
        v = tokens[i].value
        if v == opener:
            depth += 1
        elif v == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(tokens) - 1


def _split_top_commas(tokens: list[Token]) -> list[list[Token]]:
    """Split a token list on top-level commas (ignoring nested brackets)."""
    segs, cur, depth = [], [], 0
    for t in tokens:
        if t.value in "([{":
            depth += 1
        elif t.value in ")]}":
            depth -= 1
        if t.value == "," and depth == 0:
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _reconstruct(tokens: list[Token]) -> str:
    return " ".join(t.value for t in tokens)


def parse_source(text: str, filename: str = "<mem>",
                 defines: Optional[dict[str, str]] = None,
                 incdirs: Optional[list[str]] = None,
                 base_dir: str = ".") -> list[Module]:
    expanded, _ = preprocess(text, defines, incdirs, base_dir)
    tokens = tokenize(expanded)
    modules: list[Module] = []
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].value in ("module", "macromodule"):
            j = _find_endmodule(tokens, i)
            try:
                mod = _parse_module(tokens[i:j + 1], filename)
                if mod:
                    modules.append(mod)
            except Exception:  # recovery: never let one module kill the file
                pass
            i = j + 1
        else:
            i += 1
    return modules


def _find_endmodule(tokens: list[Token], start: int) -> int:
    depth = 0
    i = start
    while i < len(tokens):
        v = tokens[i].value
        if v in ("module", "macromodule"):
            depth += 1
        elif v == "endmodule":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(tokens) - 1


def _parse_module(tokens: list[Token], filename: str) -> Optional[Module]:
    # tokens[0] == 'module'
    if len(tokens) < 3:
        return None
    name = tokens[1].value
    mod = Module(name=name, file=filename, line=tokens[0].line,
                 line_end=tokens[-1].line)
    i = 2
    # optional import / #(params)
    header_params: list[Parameter] = []
    if i < len(tokens) and tokens[i].value == "#":
        # #( ... )
        if i + 1 < len(tokens) and tokens[i + 1].value == "(":
            close = _match(tokens, i + 1)
            header_params = _parse_param_list(tokens[i + 2:close], filename)
            i = close + 1
    # port list ( ... )
    port_tokens: list[Token] = []
    if i < len(tokens) and tokens[i].value == "(":
        close = _match(tokens, i)
        port_tokens = tokens[i + 1:close]
        i = close + 1
    # skip to ';'
    while i < len(tokens) and tokens[i].value != ";":
        i += 1
    body = tokens[i + 1:] if i < len(tokens) else []

    mod.params = header_params
    ansi_ports = _parse_ansi_ports(port_tokens, filename)
    if ansi_ports and any(p.direction for p in ansi_ports):
        mod.is_ansi = True
        mod.ports = ansi_ports
    else:
        mod.is_ansi = False
        # non-ANSI: names only in header; directions from body
        mod.ports = [Port(name=p.name, direction="", file=filename) for p in ansi_ports]

    _parse_body(body, mod, filename)

    # Reconcile non-ANSI port directions discovered in the body.
    if not mod.is_ansi:
        _reconcile_nonansi(mod)
    return mod


def _parse_param_list(tokens: list[Token], filename: str) -> list[Parameter]:
    params = []
    for seg in _split_top_commas(tokens):
        if not seg:
            continue
        p = _parse_one_param(seg, filename)
        if p:
            params.append(p)
    return params


def _parse_one_param(seg: list[Token], filename: str) -> Optional[Parameter]:
    is_local = False
    idx = 0
    dtype = ""
    if idx < len(seg) and seg[idx].value in ("parameter", "localparam"):
        is_local = seg[idx].value == "localparam"
        idx += 1
    # optional type / range tokens up to the name= pattern
    # find '=' position
    eq = next((k for k, t in enumerate(seg) if t.value == "="), None)
    if eq is None:
        # parameter with no default in this segment
        name = seg[-1].value if seg and seg[-1].kind == "id" else ""
        if not name:
            return None
        dtype = _reconstruct(seg[idx:-1])
        return Parameter(name=name, default_expr="", dtype=dtype.strip(),
                         is_localparam=is_local, file=filename, line=seg[0].line)
    name_tok = seg[eq - 1]
    name = name_tok.value
    dtype = _reconstruct(seg[idx:eq - 1]).strip()
    default = _reconstruct(seg[eq + 1:]).strip()
    return Parameter(name=name, default_expr=default, dtype=dtype,
                     is_localparam=is_local, file=filename, line=seg[0].line)


def _parse_ansi_ports(tokens: list[Token], filename: str) -> list[Port]:
    ports: list[Port] = []
    cur_dir, cur_type, cur_signed, cur_msb, cur_lsb, cur_width = "", "wire", False, None, None, None
    for seg in _split_top_commas(tokens):
        if not seg:
            continue
        k = 0
        direction = cur_dir
        dtype = cur_type
        signed = cur_signed
        msb, lsb, width = cur_msb, cur_lsb, cur_width
        saw_decl = False
        if seg[k].value in _DIRECTIONS:
            direction = seg[k].value
            saw_decl = True
            k += 1
            # reset inherited type/range when a new direction begins
            dtype, signed, msb, lsb, width = "wire", False, None, None, None
        # net/var type
        if k < len(seg) and seg[k].value in _NET_TYPES:
            dtype = seg[k].value
            saw_decl = True
            k += 1
        elif k < len(seg) and seg[k].kind == "id" and k + 1 < len(seg) and \
                (seg[k + 1].kind == "id" or seg[k + 1].value == "["):
            # user-defined type name (typedef) before the port name
            dtype = seg[k].value
            saw_decl = True
            k += 1
        if k < len(seg) and seg[k].value == "signed":
            signed = True
            k += 1
        if k < len(seg) and seg[k].value == "[":
            close = _match(seg, k)
            msb, lsb = _range_bounds(seg[k:close + 1])
            width = _reconstruct(seg[k:close + 1])
            saw_decl = True
            k = close + 1
        # remaining should be the port name (last id)
        name = ""
        for t in seg[k:]:
            if t.kind == "id" and t.value not in KEYWORDS:
                name = t.value
        if not name:
            continue
        ports.append(Port(name=name, direction=direction, dtype=dtype,
                          signed=signed, msb=msb, lsb=lsb, width_expr=width,
                          file=filename, line=seg[0].line))
        if saw_decl:
            cur_dir, cur_type, cur_signed = direction, dtype, signed
            cur_msb, cur_lsb, cur_width = msb, lsb, width
    return ports


def _range_bounds(tokens: list[Token]) -> tuple[Optional[str], Optional[str]]:
    # tokens include the [ ... ]
    inner = tokens[1:-1]
    # find top-level ':'
    depth = 0
    for k, t in enumerate(inner):
        if t.value in "([{":
            depth += 1
        elif t.value in ")]}":
            depth -= 1
        elif t.value == ":" and depth == 0:
            return (_reconstruct(inner[:k]).strip(), _reconstruct(inner[k + 1:]).strip())
    return (_reconstruct(inner).strip() or None, None)


def _parse_body(body: list[Token], mod: Module, filename: str) -> None:
    i, n = 0, len(body)
    while i < n:
        t = body[i]
        v = t.value
        if v in ("parameter", "localparam"):
            end = _stmt_end(body, i)
            for seg in _split_top_commas(body[i + 1:end]):
                p = _parse_one_param([t] + seg, filename)
                if p:
                    mod.params.append(p)
            i = end + 1
            continue
        if v in _DIRECTIONS:
            _parse_port_decl(body, i, mod, filename)
            i = _stmt_end(body, i) + 1
            continue
        if v == "typedef":
            end = _stmt_end(body, i)
            e = _parse_enum(body[i:end + 1], filename)
            if e:
                mod.enums.append(e)
            i = end + 1
            continue
        if v in ("always_ff", "always_comb", "always_latch", "always"):
            blk, ni = _parse_always(body, i, filename)
            if blk:
                mod.always_blocks.append(blk)
            i = ni
            continue
        if v == "assign":
            end = _stmt_end(body, i)
            ca = _parse_assign(body[i + 1:end], filename, t.line)
            if ca:
                mod.assigns.append(ca)
            i = end + 1
            continue
        if v in _NET_TYPES and t.kind == "id":
            _parse_net_decl(body, i, mod, filename)
            i = _stmt_end(body, i) + 1
            continue
        # instance? ID [#(...)] ID ( ... ) ;
        inst, ni = _try_instance(body, i, filename)
        if inst:
            mod.instances.append(inst)
            i = ni
            continue
        i += 1


def _stmt_end(tokens: list[Token], start: int) -> int:
    """Index of the ';' terminating the statement at start (depth-aware)."""
    depth = 0
    i = start
    while i < len(tokens):
        v = tokens[i].value
        if v in "([{":
            depth += 1
        elif v in ")]}":
            depth -= 1
        elif v == ";" and depth == 0:
            return i
        i += 1
    return len(tokens) - 1


def _parse_port_decl(body: list[Token], i: int, mod: Module, filename: str) -> None:
    end = _stmt_end(body, i)
    seg = body[i:end]
    direction = seg[0].value
    k = 1
    dtype = "wire"
    signed = False
    msb = lsb = width = None
    if k < len(seg) and seg[k].value in _NET_TYPES:
        dtype = seg[k].value
        k += 1
    if k < len(seg) and seg[k].value == "signed":
        signed = True
        k += 1
    if k < len(seg) and seg[k].value == "[":
        close = _match(seg, k)
        msb, lsb = _range_bounds(seg[k:close + 1])
        width = _reconstruct(seg[k:close + 1])
        k = close + 1
    names = [t.value for t in seg[k:] if t.kind == "id" and t.value not in KEYWORDS]
    for name in names:
        existing = mod.port(name)
        if existing:
            existing.direction = direction
            existing.dtype = dtype
            existing.signed = signed
            existing.msb, existing.lsb, existing.width_expr = msb, lsb, width
        else:
            mod.ports.append(Port(name=name, direction=direction, dtype=dtype,
                                  signed=signed, msb=msb, lsb=lsb,
                                  width_expr=width, file=filename, line=seg[0].line))


def _parse_net_decl(body: list[Token], i: int, mod: Module, filename: str) -> None:
    end = _stmt_end(body, i)
    seg = body[i:end]
    dtype = seg[0].value
    k = 1
    signed = False
    msb = lsb = None
    if k < len(seg) and seg[k].value == "signed":
        signed = True
        k += 1
    if k < len(seg) and seg[k].value == "[":
        close = _match(seg, k)
        msb, lsb = _range_bounds(seg[k:close + 1])
        k = close + 1
    # names up to first '=' (initializer) — collect ids at top level
    for part in _split_top_commas(seg[k:]):
        name = next((t.value for t in part if t.kind == "id"
                     and t.value not in KEYWORDS), "")
        if name and not mod.port(name):
            mod.nets.append(Net(name=name, dtype=dtype, signed=signed,
                                msb=msb, lsb=lsb, file=filename, line=seg[0].line))


def _parse_enum(seg: list[Token], filename: str) -> Optional[EnumDef]:
    # typedef enum [base] { A, B=1, C } name ;
    if "enum" not in [t.value for t in seg]:
        return None
    try:
        ei = next(k for k, t in enumerate(seg) if t.value == "enum")
    except StopIteration:
        return None
    brace = next((k for k in range(ei, len(seg)) if seg[k].value == "{"), None)
    if brace is None:
        return None
    base = _reconstruct(seg[ei + 1:brace]).strip()
    close = _match(seg, brace)
    members: dict[str, str] = {}
    for part in _split_top_commas(seg[brace + 1:close]):
        if not part:
            continue
        name = part[0].value
        val = ""
        if len(part) >= 3 and part[1].value == "=":
            val = _reconstruct(part[2:]).strip()
        members[name] = val
    tname = ""
    if close + 1 < len(seg) and seg[close + 1].kind == "id":
        tname = seg[close + 1].value
    return EnumDef(name=tname, base=base, members=members,
                   file=filename, line=seg[0].line)


def _parse_always(body: list[Token], i: int, filename: str) -> tuple[Optional[AlwaysBlock], int]:
    kind = body[i].value
    line = body[i].line
    k = i + 1
    sens: list[tuple[str, str]] = []
    clock = None
    resets: list[str] = []
    if k < len(body) and body[k].value == "@":
        k += 1
        if k < len(body) and body[k].value == "(":
            close = _match(body, k)
            sens = _parse_sens(body[k + 1:close])
            k = close + 1
    # capture the statement / begin-end block
    block_start = k
    if k < len(body) and body[k].value == "begin":
        close = _match_beginend(body, k)
        block_toks = body[block_start:close + 1]
        k = close + 1
    else:
        end = _stmt_end(body, k)
        block_toks = body[block_start:end + 1]
        k = end + 1
    text = _reconstruct(block_toks)
    # clock / reset from sens list (for edge-sensitive blocks)
    for edge, sig in sens:
        if edge == "posedge" or edge == "negedge":
            if clock is None:
                clock = sig
            else:
                resets.append(sig)
    # Heuristic: an async reset appears in the sens list AND guards the first if.
    resets = _refine_resets(text, [s for _, s in sens], clock)
    if resets and clock in resets:
        clock = next((s for e, s in sens if s not in resets), clock)
    lhs, rhs = _lhs_rhs(text)
    blk = AlwaysBlock(kind=kind, sens=sens, clock=clock, resets=resets,
                      lhs=lhs, rhs=rhs, body=text, file=filename, line=line)
    return blk, k


def _parse_sens(tokens: list[Token]) -> list[tuple[str, str]]:
    sens = []
    for seg in re.split(r"\bor\b", _reconstruct(tokens)):
        seg = seg.strip()
        if not seg or seg == "*":
            continue
        m = re.match(r"(posedge|negedge)\s+([A-Za-z_]\w*)", seg)
        if m:
            sens.append((m.group(1), m.group(2)))
        else:
            m2 = re.match(r"([A-Za-z_]\w*)", seg)
            if m2:
                sens.append(("level", m2.group(1)))
    return sens


def _refine_resets(text: str, sens_signals: list[str], clock: Optional[str]) -> list[str]:
    resets = []
    for sig in sens_signals:
        if sig == clock:
            continue
        # reset if it guards the block: if (!rst_n) / if (rst) / if (rst_n == 0)
        if re.search(rf"if\s*\(\s*!?\s*{re.escape(sig)}\b", text) or \
           re.search(rf"if\s*\(\s*~?\s*{re.escape(sig)}\b", text):
            resets.append(sig)
        elif re.search(r"rst|reset|clr|clear", sig, re.I):
            resets.append(sig)
    return resets


_ASSIGN_RE = re.compile(r"([A-Za-z_]\w*(?:\s*\[[^\]]*\])?)\s*(<=|=)(?!=)\s*")


def _lhs_rhs(text: str) -> tuple[set[str], set[str]]:
    lhs: set[str] = set()
    rhs: set[str] = set()
    # Assignments
    for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(<=|=)(?!=)([^;]*)", text):
        lhs.add(m.group(1))
        rhs |= signal_names(m.group(3))
    # case targets read the selector
    for m in re.finditer(r"\bcase[zx]?\s*\(([^)]*)\)", text):
        rhs |= signal_names(m.group(1))
    for m in re.finditer(r"\bif\s*\(([^)]*)\)", text):
        rhs |= signal_names(m.group(1))
    lhs.discard("")
    return lhs, rhs


def _match_beginend(tokens: list[Token], i: int) -> int:
    depth = 0
    while i < len(tokens):
        v = tokens[i].value
        if v == "begin":
            depth += 1
        elif v == "end":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(tokens) - 1


def _parse_assign(seg: list[Token], filename: str, line: int) -> Optional[ContinuousAssign]:
    text = _reconstruct(seg)
    m = re.match(r"\s*(.+?)=(?!=)(.*)", text)
    if not m:
        return None
    lhs_txt, rhs_txt = m.group(1), m.group(2)
    lhs_sig = signal_names(lhs_txt)
    return ContinuousAssign(lhs=lhs_txt.strip(), rhs=rhs_txt.strip(),
                            lhs_signals=lhs_sig, rhs_signals=signal_names(rhs_txt),
                            file=filename, line=line)


def _try_instance(body: list[Token], i: int, filename: str) -> tuple[Optional[Instance], int]:
    t = body[i]
    if t.kind != "id" or t.value in _NON_INSTANCE_HEADS:
        return None, i + 1
    module_type = t.value
    k = i + 1
    param_overrides: dict[str, str] = {}
    if k < len(body) and body[k].value == "#":
        if k + 1 < len(body) and body[k + 1].value == "(":
            close = _match(body, k + 1)
            param_overrides = _parse_conns(body[k + 2:close])
            k = close + 1
        else:
            return None, i + 1
    # instance name
    if k >= len(body) or body[k].kind != "id" or body[k].value in KEYWORDS:
        return None, i + 1
    inst_name = body[k].value
    k += 1
    # optional unpacked array dim on instance name
    if k < len(body) and body[k].value == "[":
        k = _match(body, k) + 1
    if k >= len(body) or body[k].value != "(":
        return None, i + 1
    close = _match(body, k)
    conns = _parse_conns(body[k + 1:close])
    ordered = all(re.fullmatch(r"p\d+", key) for key in conns) and bool(conns)
    end = close + 1
    # require terminating ';'
    if end < len(body) and body[end].value == ";":
        end += 1
    inst = Instance(inst_name=inst_name, module_type=module_type,
                    param_overrides=param_overrides, connections=conns,
                    ordered=ordered, file=filename, line=t.line)
    return inst, end


def _parse_conns(tokens: list[Token]) -> dict[str, str]:
    conns: dict[str, str] = {}
    for idx, seg in enumerate(_split_top_commas(tokens)):
        if not seg:
            continue
        if seg[0].value == ".":
            if len(seg) >= 2:
                port = seg[1].value
                if port == "*":
                    conns[".*"] = "*"
                    continue
                # .port(expr) or .port  (implicit)
                expr = ""
                if len(seg) >= 4 and seg[2].value == "(":
                    close = _match(seg, 2)
                    expr = _reconstruct(seg[3:close]).strip()
                else:
                    expr = port  # implicit .name
                conns[port] = expr
        else:
            conns[f"p{idx}"] = _reconstruct(seg).strip()
    return conns


def _reconcile_nonansi(mod: Module) -> None:
    # Drop header placeholder ports that never received a direction and keep the
    # body-declared ones (already merged by name in _parse_port_decl).
    mod.ports = [p for p in mod.ports if p.direction] or mod.ports
