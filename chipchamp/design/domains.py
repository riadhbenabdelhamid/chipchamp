"""Clock- and reset-domain inference (SPEC §8.3, FR-DB-04).

Per module, each sequential block (``always_ff``/``always @(posedge ...)``)
defines a domain named by its clock. Every signal the block assigns belongs to
that domain. ``DESIGN.md`` annotations override inference (applied by the DB).
The result feeds CDC-lite checks and waveform debugging.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Module


@dataclass
class DomainMap:
    module: str
    signal_domain: dict[str, str] = field(default_factory=dict)  # signal -> clock
    signal_reset: dict[str, str] = field(default_factory=dict)   # signal -> reset
    clocks: set[str] = field(default_factory=set)
    resets: set[str] = field(default_factory=set)
    # crossings: (signal, from_clock, to_clock, sink_signal)
    crossings: list[tuple[str, str, str, str]] = field(default_factory=list)


def infer_domains(mod: Module, overrides: dict[str, str] | None = None) -> DomainMap:
    overrides = overrides or {}
    dm = DomainMap(module=mod.name)
    for blk in mod.always_blocks:
        if blk.kind == "always_comb" or blk.kind == "always_latch":
            continue
        if not blk.clock:
            continue
        dm.clocks.add(blk.clock)
        for r in blk.resets:
            dm.resets.add(r)
        for sig in blk.lhs:
            dm.signal_domain[sig] = overrides.get(sig, blk.clock)
            if blk.resets:
                dm.signal_reset[sig] = blk.resets[0]
    dm.signal_domain.update(overrides)

    # Intra-module CDC: a sequential block in clock A reads a register that lives
    # in clock B (a crossing) without an intervening synchronizer name hint.
    for blk in mod.always_blocks:
        if blk.kind in ("always_comb", "always_latch") or not blk.clock:
            continue
        sink_clk = blk.clock
        for src in blk.rhs:
            src_clk = dm.signal_domain.get(src)
            if src_clk and src_clk != sink_clk:
                sinks = [s for s in blk.lhs] or ["<comb>"]
                for sink in sinks:
                    dm.crossings.append((src, src_clk, sink_clk, sink))
    return dm
