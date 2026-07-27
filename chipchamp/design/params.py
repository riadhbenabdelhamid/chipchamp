"""Safe evaluation of Verilog constant expressions for parameter resolution.

Used to turn parameter override expressions (``.WIDTH(DATA_W)``) into concrete
values during hierarchy elaboration (FR-DB-02). Falls back to the raw expression
string when a value cannot be reduced to an integer (e.g. it depends on an
unresolved parameter), so the hierarchy is still usable.
"""
from __future__ import annotations

import ast
import math
import re
from typing import Optional


def _clog2(x: int) -> int:
    x = int(x)
    return 0 if x <= 1 else math.ceil(math.log2(x))


_FUNCS = {"clog2": _clog2}

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name,
            ast.Call, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div,
            ast.FloorDiv, ast.Mod, ast.Pow, ast.LShift, ast.RShift,
            ast.BitOr, ast.BitAnd, ast.BitXor, ast.USub, ast.UAdd, ast.Invert)


def _preclean(expr: str) -> str:
    expr = expr.strip()
    # $clog2(x) -> clog2(x)
    expr = expr.replace("$clog2", "clog2")
    # sized/based literals: 8'd12, 'h1F, 4'b1010 -> integer
    def repl(m: re.Match) -> str:
        base = m.group("base").lower()
        digits = m.group("digits").replace("_", "")
        try:
            return str(int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[base]))
        except ValueError:
            return "0"
    expr = re.sub(r"(?:\d+)?\s*'\s*[sS]?(?P<base>[bBoOdDhH])(?P<digits>[0-9a-fA-F_]+)", repl, expr)
    # width-cast like AW'(x) -> (x)
    expr = re.sub(r"[A-Za-z_]\w*\s*'\s*\(", "(", expr)
    # strip digit-group underscores (1_000) WITHOUT touching identifier
    # underscores (DATA_W) — only remove '_' that sits between two digits.
    expr = re.sub(r"(?<=[0-9])_(?=[0-9])", "", expr)
    return expr


def eval_const(expr: str, namespace: dict[str, object]) -> Optional[int]:
    if expr is None:
        return None
    cleaned = _preclean(str(expr))
    if not cleaned:
        return None
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            return None
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
                return None
    ns = {"clog2": _clog2}
    for k, v in namespace.items():
        if isinstance(v, int):
            ns[k] = v
        elif isinstance(v, str):
            iv = eval_const(v, {kk: vv for kk, vv in namespace.items() if kk != k})
            if iv is not None:
                ns[k] = iv
    try:
        val = eval(compile(tree, "<param>", "eval"), {"__builtins__": {}}, ns)
        return int(val)
    except Exception:
        return None
