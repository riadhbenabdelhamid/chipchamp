"""Fan-in / fan-out cone extraction (SPEC §8.9 L3, `design.cone`).

Instead of dumping whole files into model context, the agent asks for the pruned
logic that feeds (or is fed by) a signal. This returns a self-contained,
compilable-order source slice plus the list of signals encountered and their
domains — the debugging primitive behind playbook P2.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Module


@dataclass
class ConeSlice:
    signal: str
    direction: str
    depth: int
    statements: list[str] = field(default_factory=list)  # source lines w/ file:line
    signals: list[dict] = field(default_factory=list)     # {name, domain, drivers}
    truncated: bool = False


def _drivers_of(mod: Module, sig: str) -> list[tuple[str, str, set[str]]]:
    """Return (source_text, location, upstream_signals) for each driver of sig."""
    out = []
    base = sig.split("[")[0].split(".")[0]
    for ca in mod.assigns:
        if base in {s.split("[")[0] for s in ca.lhs_signals}:
            out.append((f"assign {ca.lhs} = {ca.rhs};", f"{ca.file}:{ca.line}", ca.rhs_signals))
    for blk in mod.always_blocks:
        if base in {s.split("[")[0] for s in blk.lhs}:
            head = f"{blk.kind}" + (f" @({', '.join(e + ' ' + s for e, s in blk.sens)})" if blk.sens else "")
            out.append((f"{head} ... // assigns {base}", f"{blk.file}:{blk.line}", blk.rhs))
    return out


def _loads_of(mod: Module, sig: str) -> list[tuple[str, str, set[str]]]:
    out = []
    base = sig.split("[")[0]
    for ca in mod.assigns:
        if base in {s.split("[")[0] for s in ca.rhs_signals}:
            out.append((f"assign {ca.lhs} = {ca.rhs};", f"{ca.file}:{ca.line}", ca.lhs_signals))
    for blk in mod.always_blocks:
        if base in {s.split("[")[0] for s in blk.rhs}:
            head = f"{blk.kind}"
            out.append((f"{head} ... // reads {base}", f"{blk.file}:{blk.line}", blk.lhs))
    return out


def _inst_drivers(mod: Module, sig: str, port_dir=None):
    """Instance output connections that drive `sig` (crosses module boundaries).

    `port_dir(module_type, port)` -> 'input'|'output'|'inout'|None lets us tell a
    driving (output) connection from a load (input) connection."""
    out = []
    base = sig.split("[")[0]
    for inst in mod.instances:
        for port, expr in inst.connections.items():
            if port in (".*", ""):
                continue
            from .parser import signal_names
            if base in {s.split("[")[0] for s in signal_names(expr)}:
                d = port_dir(inst.module_type, port) if port_dir else None
                if d == "output" or d is None:
                    out.append((
                        f"{inst.module_type} {inst.inst_name} ( .{port}({expr}) ) "
                        f"// {inst.inst_name} drives {base}",
                        f"{inst.file}:{inst.line}", set()))
    return out


def extract_cone(mod: Module, signal: str, direction: str = "fanin",
                 depth: int = 3, domain_of=None, max_stmts: int = 60,
                 port_dir=None) -> ConeSlice:
    slice_ = ConeSlice(signal=signal, direction=direction, depth=depth)
    seen_sig: set[str] = set()
    seen_stmt: set[str] = set()
    frontier = {signal.split(".")[-1].split("[")[0]}
    step = _drivers_of if direction == "fanin" else _loads_of
    for _ in range(depth):
        nxt: set[str] = set()
        for s in list(frontier):
            if s in seen_sig:
                continue
            seen_sig.add(s)
            dom = domain_of(s) if domain_of else None
            drivers = step(mod, s)
            if direction == "fanin":
                drivers = drivers + _inst_drivers(mod, s, port_dir)
            slice_.signals.append({"name": s, "domain": dom,
                                   "drivers": [loc for _, loc, _ in drivers]})
            for text, loc, ups in drivers:
                key = f"{loc}|{text}"
                if key not in seen_stmt:
                    seen_stmt.add(key)
                    slice_.statements.append(f"// {loc}\n{text}")
                    if len(slice_.statements) >= max_stmts:
                        slice_.truncated = True
                        return slice_
                nxt |= {u.split("[")[0] for u in ups}
        frontier = nxt - seen_sig
        if not frontier:
            break
    return slice_
