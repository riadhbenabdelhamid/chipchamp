"""Mutation operators for ChipchampBench B1 (SPEC §17.1).

Realistic RTL defect classes injected into known-good source: operator swaps,
comparison flips, off-by-one constants, wrong-signal substitutions, reset-value
corruption. Each mutator returns (mutated_text, description) or None when it
doesn't apply — the harness collects every applicable (mutator, site) pair.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Mutant:
    id: str
    file: str
    line: int
    description: str
    original: str
    mutated: str


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _swap_at(text: str, pattern: str, repl: str, occurrence: int) -> Optional[tuple[str, int]]:
    matches = list(re.finditer(pattern, text))
    if occurrence >= len(matches):
        return None
    m = matches[occurrence]
    return text[:m.start()] + repl + text[m.end():], _line_of(text, m.start())


# (name, finder-pattern, replacement, description)
_OPERATOR_MUTATIONS = [
    ("plus-to-minus", r"(?<=[\w\s])\+(?=\s*1'b1)", "-", "increment becomes decrement"),
    ("eq-to-neq", r"==(?!=)", "!=", "equality inverted"),
    ("and-to-or", r"(?<=[\w\s])&(?![&=])(?=[\s\w~])", "|", "bitwise AND becomes OR"),
    ("lt-to-lte", r"(?<=[\w\s])<(?![<=])(?=[\s\w])", "<=", "comparison off-by-one"),
    ("invert-condition", r"if \(!([a-z_]\w*)\)", r"if (\1)", "condition polarity flipped"),
]


def generate_mutants(text: str, filename: str, per_op: int = 2) -> list[Mutant]:
    mutants: list[Mutant] = []
    for name, pat, repl, desc in _OPERATOR_MUTATIONS:
        for occ in range(per_op):
            if name == "invert-condition":
                matches = list(re.finditer(pat, text))
                if occ >= len(matches):
                    continue
                m = matches[occ]
                mutated = text[:m.start()] + re.sub(pat, repl, m.group(0)) + text[m.end():]
                line = _line_of(text, m.start())
            else:
                out = _swap_at(text, pat, repl, occ)
                if out is None:
                    continue
                mutated, line = out
            if mutated == text:
                continue
            mutants.append(Mutant(
                id=f"{name}-{occ}", file=filename, line=line,
                description=desc, original=text, mutated=mutated))
    return mutants
