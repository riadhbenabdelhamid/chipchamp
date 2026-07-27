"""Finite-state-machine extraction (SPEC §8.3, FR group B `design.fsm`).

Recognizes the common two-process idiom: an ``always_ff`` that registers a state
variable (``state <= next``) plus an ``always_comb`` that computes ``next`` in a
``case (state)``. States come from the enum typedef; transitions from the
per-state assignments to the next-state variable. Encoding is read off the enum
member values (one-hot / binary / gray heuristics).
"""
from __future__ import annotations

import re

from .model import FSM, Module


def _detect_encoding(values: list[str]) -> str:
    bits = []
    for v in values:
        m = re.search(r"'[bB]([01]+)", v)
        if not m:
            return "unknown"
        bits.append(m.group(1))
    if not bits:
        return "unknown"
    if all(b.count("1") == 1 for b in bits):
        return "onehot"
    # gray: adjacent differ by one bit
    def ham(a, b):
        a = a.zfill(max(len(a), len(b)))
        b = b.zfill(max(len(a), len(b)))
        return sum(x != y for x, y in zip(a, b))
    if len(bits) > 1 and all(ham(bits[i], bits[i + 1]) == 1 for i in range(len(bits) - 1)):
        return "gray"
    return "binary"


def extract_fsms(mod: Module) -> list[FSM]:
    fsms: list[FSM] = []
    # Candidate state enums: enum typedefs whose members look like states.
    state_members: dict[str, dict[str, str]] = {}
    for e in mod.enums:
        state_members[e.name or f"<anon@{e.line}>"] = e.members

    # Find the state register: always_ff assigning X <= next/nxt/ns pattern
    for blk in mod.always_blocks:
        if blk.kind not in ("always_ff", "always"):
            continue
        pairs = re.findall(r"([A-Za-z_]\w*)\s*<=\s*([A-Za-z_]\w*)\s*;", blk.body)
        if not pairs:
            continue
        # the members of whichever enum this block resets to
        members: dict[str, str] = {}
        for mem in state_members.values():
            if any(rhs in mem for _, rhs in pairs):
                members = mem
                break
        if not members:
            members = next(iter(state_members.values()), {})
        if not members:
            continue
        # state_reg is the LHS (assume one); next_reg is the RHS that is a signal,
        # NOT an enum member (an enum member on the RHS is a reset/direct value).
        state_reg = pairs[0][0]
        next_candidates = [rhs for lhs, rhs in pairs
                           if rhs not in members and lhs == state_reg]
        next_reg = next_candidates[0] if next_candidates else None
        if next_reg is None:
            continue

        # transitions from the comb block computing next_reg
        transitions: list[tuple[str, str, str]] = []
        comb = _find_next_logic(mod, next_reg)
        if comb:
            transitions = _extract_transitions(comb.body, next_reg, list(members))
        enc = _detect_encoding(list(members.values()))
        fsms.append(FSM(module=mod.name, state_reg=state_reg, next_reg=next_reg,
                        states=list(members), transitions=transitions,
                        encoding=enc, file=mod.file, line=blk.line))
    return fsms


def _find_next_logic(mod: Module, next_reg: str):
    for blk in mod.always_blocks:
        if next_reg in blk.lhs:
            return blk
    return None


def _extract_transitions(body: str, next_reg: str, states: list[str]) -> list[tuple[str, str, str]]:
    """Very small case-based transition extractor. Splits the case body by state
    labels and records ``next = TARGET`` assignments with their guarding
    condition text (best-effort)."""
    transitions: list[tuple[str, str, str]] = []
    # locate 'case (...) ... endcase'
    cm = re.search(r"case[zx]?\s*\(.*?\)(.*)endcase", body, re.S)
    region = cm.group(1) if cm else body
    # split into "LABEL : ..." arms
    arm_re = re.compile(r"(" + "|".join(re.escape(s) for s in states + ["default"]) + r")\s*:")
    marks = list(arm_re.finditer(region))
    for idx, mk in enumerate(marks):
        label = mk.group(1)
        seg = region[mk.end(): marks[idx + 1].start() if idx + 1 < len(marks) else len(region)]
        # assignments to next_reg within the arm, with nearest if-condition
        for am in re.finditer(rf"(?:if\s*\(([^)]*)\)\s*)?{re.escape(next_reg)}\s*=\s*([A-Za-z_]\w*)", seg):
            cond = (am.group(1) or "").strip() or "-"
            target = am.group(2)
            if target in states and label != "default":
                transitions.append((label, target, cond))
    return transitions
