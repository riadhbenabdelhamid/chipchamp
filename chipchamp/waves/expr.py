"""Tiny 4-state signal-expression evaluator for ``wave.when`` (SPEC §8.6 FR-WAVE-04).

Supports the Verilog-flavored subset the agent needs to search a dump for a
condition: ``&& || ! == != < > <= >= & | ^ ~ + -`` and based literals, over
signal values sampled at a time point. A value containing x/z evaluates as
"unknown"; comparisons against it yield False unless the expression explicitly
tests for x (via the ``$isunknown(sig)`` helper).
"""
from __future__ import annotations

import ast
import re
from typing import Callable, Optional

_BASED = re.compile(r"(?:\d+)?\s*'\s*[sS]?(?P<b>[bBoOdDhH])(?P<d>[0-9a-fA-FxXzZ_]+)")


def _lit(m: re.Match) -> str:
    base = m.group("b").lower()
    digits = m.group("d").replace("_", "")
    if any(c in "xXzZ" for c in digits):
        return "None"
    try:
        return str(int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[base]))
    except ValueError:
        return "None"


class SignalExpr:
    def __init__(self, expr: str):
        self.raw = expr
        self.names = sorted(set(re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", expr))
                            - {"and", "or", "not"})
        py = expr
        py = re.sub(r"\$isunknown\s*\(\s*([\w.]+)\s*\)", r"__isx('\1')", py)
        py = _BASED.sub(_lit, py)
        py = py.replace("&&", " and ").replace("||", " or ")
        # unary ! -> not ; keep bitwise & | ^ ~ as python ops
        py = re.sub(r"!(?!=)", " not ", py)
        py = re.sub(r"===", "==", py).replace("!==", "!=")
        self.py = py

    def eval(self, value_of: Callable[[str], Optional[int]],
             isx_of: Callable[[str], bool]) -> Optional[bool]:
        ns = {"__isx": isx_of}
        for name in self.names:
            if name.replace(".", "_").isidentifier() and "." not in name:
                ns[name] = value_of(name)
        # dotted names: expose via a mapping isn't valid python; rewrite them
        py = self.py
        for name in self.names:
            if "." in name:
                safe = "sig_" + re.sub(r"\W", "_", name)
                py = py.replace(name, safe)
                ns[safe] = value_of(name)
        try:
            tree = ast.parse(py, mode="eval")
        except SyntaxError:
            return None
        try:
            val = eval(compile(tree, "<expr>", "eval"),
                       {"__builtins__": {}}, ns)
        except TypeError:
            # comparison involving None (x/z) -> unknown -> treat as False
            return False
        except Exception:
            return None
        return bool(val) if val is not None else False
