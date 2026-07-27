"""Pragmatic SystemVerilog preprocessor + tokenizer.

A full IEEE-1800 front end is out of scope for the platform substrate; slang is
the production front end (SPEC §8.3). This module is the portable, dependency-free
fallback ("degraded mode" — FR-DB-07) that still recovers modules, ports, params,
instances, procedural blocks and continuous assigns from a synthesizable subset,
with accurate line numbers for ``file:line`` citations.

The preprocessor preserves newlines when it strips comments and expands macros so
that reported line numbers stay faithful to the original source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "logic", "wire", "reg",
    "bit", "signed", "unsigned", "parameter", "localparam", "assign", "always",
    "always_ff", "always_comb", "always_latch", "initial", "begin", "end",
    "if", "else", "case", "casez", "casex", "endcase", "for", "while", "posedge",
    "negedge", "or", "and", "typedef", "enum", "struct", "packed", "genvar",
    "generate", "endgenerate", "function", "endfunction", "task", "endtask",
    "interface", "endinterface", "modport", "package", "endpackage", "import",
    "return", "default", "int", "integer", "byte", "shortint", "longint",
    "unique", "priority", "static", "automatic", "const", "var", "wor", "wand",
}


def strip_comments(text: str) -> str:
    """Remove // and /* */ comments, replacing them with spaces but keeping
    newlines so downstream line numbers stay correct."""
    out = []
    i, n = 0, len(text)
    state = "code"  # code | line_comment | block_comment | string
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line_comment"
                out.append("  ")
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block_comment"
                out.append("  ")
                i += 2
                continue
            if c == '"':
                state = "string"
                out.append(c)
                i += 1
                continue
            out.append(c)
            i += 1
        elif state == "line_comment":
            if c == "\n":
                state = "code"
                out.append(c)
            else:
                out.append(" ")
            i += 1
        elif state == "block_comment":
            if c == "*" and nxt == "/":
                state = "code"
                out.append("  ")
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
        elif state == "string":
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                state = "code"
            i += 1
    return "".join(out)


_DEFINE_RE = re.compile(r"^[ \t]*`define[ \t]+(\w+)(\([^)]*\))?[ \t]*(.*)$")


def preprocess(text: str, defines: Optional[dict[str, str]] = None,
               incdirs: Optional[list[str]] = None, base_dir: str = ".",
               _depth: int = 0) -> tuple[str, dict[str, str]]:
    """Handle `define / `ifdef / `ifndef / `else / `elsif / `endif / `include and
    object-like macro substitution. Function-like macros are left textually inert
    (their call sites are preserved). Returns (expanded_text, final_defines)."""
    defines = dict(defines or {})
    incdirs = incdirs or []
    text = strip_comments(text)
    # Join line continuations in `define bodies.
    text = re.sub(r"\\\n", " ", text)

    out_lines: list[str] = []
    # stack of (currently_active, any_branch_taken)
    cond_stack: list[tuple[bool, bool]] = []

    def active() -> bool:
        return all(a for a, _ in cond_stack)

    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("`ifdef") or stripped.startswith("`ifndef"):
            name = stripped.split()[1] if len(stripped.split()) > 1 else ""
            cond = (name in defines) if stripped.startswith("`ifdef") else (name not in defines)
            cond_stack.append((cond and (active() if cond_stack else True), cond))
            out_lines.append("")
            continue
        if stripped.startswith("`elsif"):
            if cond_stack:
                _, taken = cond_stack[-1]
                name = stripped.split()[1] if len(stripped.split()) > 1 else ""
                cond = (name in defines) and not taken
                parent_active = all(a for a, _ in cond_stack[:-1]) if len(cond_stack) > 1 else True
                cond_stack[-1] = (cond and parent_active, taken or cond)
            out_lines.append("")
            continue
        if stripped.startswith("`else"):
            if cond_stack:
                prev_active, taken = cond_stack[-1]
                parent_active = all(a for a, _ in cond_stack[:-1]) if len(cond_stack) > 1 else True
                cond_stack[-1] = ((not taken) and parent_active, True)
            out_lines.append("")
            continue
        if stripped.startswith("`endif"):
            if cond_stack:
                cond_stack.pop()
            out_lines.append("")
            continue
        if not active():
            out_lines.append("")
            continue
        m = _DEFINE_RE.match(raw)
        if m:
            name, args, body = m.group(1), m.group(2), m.group(3)
            if not args:  # object-like only
                defines[name] = body.strip()
            out_lines.append("")
            continue
        if stripped.startswith("`undef"):
            parts = stripped.split()
            if len(parts) > 1:
                defines.pop(parts[1], None)
            out_lines.append("")
            continue
        if stripped.startswith("`include") and _depth < 8:
            m2 = re.search(r'`include\s+"([^"]+)"', stripped)
            inc_text = ""
            if m2:
                fname = m2.group(1)
                for d in [base_dir, *incdirs]:
                    cand = Path(d) / fname
                    if cand.exists():
                        try:
                            sub, defines = preprocess(cand.read_text(errors="replace"),
                                                      defines, incdirs, str(cand.parent),
                                                      _depth + 1)
                            inc_text = sub
                        except OSError:
                            pass
                        break
            # Keep line count stable: collapse include into a single line.
            out_lines.append(" ".join(inc_text.split("\n")))
            continue
        out_lines.append(raw)

    expanded = "\n".join(out_lines)
    expanded = _expand_object_macros(expanded, defines)
    return expanded, defines


def _expand_object_macros(text: str, defines: dict[str, str]) -> str:
    if not defines:
        return text
    # Iteratively expand `NAME references (bounded to avoid runaway recursion).
    for _ in range(6):
        changed = False

        def repl(m: re.Match) -> str:
            nonlocal changed
            name = m.group(1)
            if name in defines:
                changed = True
                return defines[name]
            return m.group(0)

        text = re.sub(r"`(\w+)", repl, text)
        if not changed:
            break
    return text


@dataclass
class Token:
    kind: str  # id | num | str | punct
    value: str
    line: int
    col: int


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<num>\d[\d_]*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+ | \d[\d_]*(?:\.\d+)? | '[bBoOdDhH][0-9a-fA-FxXzZ_?]+)
  | (?P<id>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<punct>::|<<|>>|<=|>=|==|!=|&&|\|\||\+:|-:|\+|\-|\*|/|%|=|<|>|\(|\)|\[|\]|\{|\}|,|;|:|\.|@|\#|\?|~|\^|&|\||!)
  | (?P<other>.)
    """,
    re.VERBOSE,
)


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    line, col = 1, 1
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        val = m.group()
        if kind == "ws":
            nl = val.count("\n")
            if nl:
                line += nl
                col = len(val) - val.rfind("\n")
            else:
                col += len(val)
            continue
        if kind == "other":
            col += len(val)
            continue
        tokens.append(Token(kind, val, line, col))
        col += len(val)
    return tokens
