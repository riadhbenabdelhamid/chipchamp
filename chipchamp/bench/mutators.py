"""Mutation operators for ChipchampBench B1 (SPEC §17.1).

Realistic RTL defect classes injected into known-good source: operator swaps,
comparison flips, off-by-one constants, wrong-signal substitutions, reset-value
corruption. Each mutator returns (mutated_text, description) or None when it
doesn't apply — the harness collects every applicable (mutator, site) pair.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


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


# (name, finder-pattern, replacement, description)
_OPERATOR_MUTATIONS = [
    ("plus-to-minus", r"(?<=[\w\s\]])\+(?=\s*(?:1'b1\b|1\b(?!')))", "-", "increment becomes decrement"),
    ("eq-to-neq", r"(?<![=!])==(?!=)", "!=", "equality inverted"),
    ("and-to-or", r"(?<=[\w\s\]])&(?![&=])(?=[\s\w~])", "|", "bitwise AND becomes OR"),
    ("lt-to-lte", r"(?<=[\w\s])<(?![<=])(?=[\s\w])", "<=", "comparison off-by-one"),
    ("invert-condition", r"if \(!([a-z_]\w*)\)", r"if (\1)", "condition polarity flipped"),
]


def code_mask(text: str) -> str:
    """Hide comments, strings and escaped identifiers without moving source offsets."""
    return re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\\[^\s]+',
                  lambda m: re.sub(r"[^\r\n]", " ", m.group()), text)


def generate_mutants(text: str, filename: str, per_op: int = 2, *,
                     lines: set[int] | None = None, limit: int | None = None) -> list[Mutant]:
    """Deterministic single-site mutations, optionally restricted to current lines.

    Filter sites before applying the per-operator cap: a changed site near the
    end of a file must not disappear behind earlier, unchanged occurrences.
    These are lexical operators, not a claim of semantic inequivalence.
    """
    mutants: list[Mutant] = []
    masked = code_mask(text)
    for name, pat, repl, desc in _OPERATOR_MUTATIONS:
        accepted = 0
        for occ, m in enumerate(re.finditer(pat, masked)):
            line = _line_of(text, m.start())
            if lines is not None and line not in lines:
                continue
            if accepted >= per_op or (limit is not None and len(mutants) >= limit):
                break
            mutated = text[:m.start()] + m.expand(repl) + text[m.end():]
            if mutated == text:
                continue
            accepted += 1
            mutants.append(Mutant(
                id=f"{name}-{occ}", file=filename, line=line,
                description=desc, original=text, mutated=mutated))
    return mutants
