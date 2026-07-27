"""Retired-instruction traces, and the first-divergence diff over them.

This is the payoff of the RISC-V vertical. A core that "hangs on test 7" tells
you nothing; a core whose PC stream leaves the reference model's at a specific
instruction tells you everything. Both sides are normalized to
:class:`TraceStep` — an ordered stream of retired PCs, optionally carrying the
instruction word, disassembly and architectural writeback — and
:func:`diff_traces` reports the FIRST place they part company.

Two producers are understood out of the box, plus a permissive fallback:

* **spike** ``-l`` commit log — ``core   0: 0x0000000080000000 (0x00000297) auipc t0, 0x0``
* **gdb/binutils sim** ``--trace-insn`` — ``insn:     0x010000    -addi ra, zero, 0x5;``
* **RTL** — any log line exposing a retired PC, e.g. ``[TRACE] pc=0x80000004
  instr=0x00500093``, which is what a core testbench typically prints.

Alignment is by *sequence*, not by PC value: a core that executes an extra
instruction, or skips one, must be reported at the point the streams diverge,
not silently re-synced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TraceStep:
    pc: int
    insn: int | None = None          # raw instruction word, when known
    disasm: str = ""
    # architectural writeback, when the producer reports it (spike does)
    reg: str = ""
    value: int | None = None
    # Which hardware thread retired it. A BARREL-MULTITHREADED core interleaves
    # T threads round-robin, so its retired stream is T independent streams
    # multiplexed together — comparing it as one against a single-hart model
    # diverges at the second instruction. Demultiplex before diffing.
    hart: int = 0
    index: int = 0                   # position in the stream

    def key(self) -> tuple:
        """What two producers can be compared on. The instruction word is
        included only when BOTH sides have it — a core that doesn't log it must
        not fail the comparison for that reason alone."""
        return (self.pc, self.insn)


# `core   0: 0x0000000080000000 (0x00000297) auipc t0, 0x0`
_SPIKE = re.compile(
    r"^core\s+(?P<hart>\d+):\s+0x(?P<pc>[0-9a-fA-F]+)\s+\((?P<insn>0x[0-9a-fA-F]+)\)"
    r"\s*(?P<dis>.*)$")
# `core   0: 3 0x0000000080000004 (0x00500093) x1  0x0000000000000005`
_SPIKE_WB = re.compile(
    r"^core\s+(?P<hart>\d+):\s+\d+\s+0x(?P<pc>[0-9a-fA-F]+)\s+\((?P<insn>0x[0-9a-fA-F]+)\)"
    r"(?:\s+(?P<reg>[xf]\d+|\w+)\s+0x(?P<val>[0-9a-fA-F]+))?")
# `insn:     0x010000                       -addi ra, zero, 0x5;  // ra = …`
_GDBSIM = re.compile(
    r"^insn:\s+0x(?P<pc>[0-9a-fA-F]+)\s+-(?P<dis>[^;]*)")
# permissive RTL: any line with a pc=, optionally instr=/insn=
_RTL_PC = re.compile(r"\bpc\s*[=:]\s*(?:0x)?(?P<pc>[0-9a-fA-F]+)", re.I)
_RTL_INSN = re.compile(r"\b(?:instr|insn|inst)\s*[=:]\s*(?:0x)?(?P<insn>[0-9a-fA-F]+)",
                       re.I)
# a barrel core's testbench identifies the retiring thread. Captured with its
# prefix: a PC is conventionally hex, but a thread id is printed with %0d, so
# reading it as hex silently renumbered threads 10-15 as 16-21.
_RTL_HART = re.compile(
    r"\b(?:hart|thread|tid|thread_index|th)\s*[=:]\s*"
    r"(?P<pfx>0x)?(?P<hart>[0-9a-fA-F]+)", re.I)


def parse_spike_trace(text: str, limit: int = 0) -> list[TraceStep]:
    """Spike's log. With ``--log-commits`` each instruction produces TWO lines —
    the disassembly, then a commit line carrying the architectural writeback::

        core   0: 0x80000000 (0x00500093) li      ra, 5
        core   0: 3 0x80000000 (0x00500093) x1  0x00000005

    The commit line is folded into the step the disassembly opened. Counting it
    as a second instruction would double the stream and desync lockstep
    alignment against a core that retires each instruction once."""
    steps: list[TraceStep] = []
    for line in text.splitlines():
        wb = _SPIKE_WB.match(line)
        if wb:
            g = wb.groupdict()
            pc = int(g["pc"], 16)
            insn = int(g["insn"], 16) if g.get("insn") else None
            prev = steps[-1] if steps else None
            if prev is not None and prev.pc == pc and prev.insn == insn \
                    and prev.hart == int(g.get("hart") or 0) and not prev.reg:
                if g.get("reg"):                    # fold into the open step
                    prev.reg, prev.value = g["reg"], int(g["val"], 16)
                continue
            step = TraceStep(pc=pc, insn=insn, index=len(steps),
                             hart=int(g.get("hart") or 0))
            if g.get("reg"):
                step.reg, step.value = g["reg"], int(g["val"], 16)
        else:
            m = _SPIKE.match(line)
            if not m:
                continue
            step = TraceStep(pc=int(m.group("pc"), 16),
                             insn=int(m.group("insn"), 16),
                             disasm=(m.group("dis") or "").strip(),
                             hart=int(m.group("hart") or 0),
                             index=len(steps))
        steps.append(step)
        if limit and len(steps) >= limit:
            break
    return steps


def parse_gdbsim_trace(text: str, limit: int = 0) -> list[TraceStep]:
    steps: list[TraceStep] = []
    for line in text.splitlines():
        m = _GDBSIM.match(line)
        if not m:
            continue
        steps.append(TraceStep(pc=int(m.group("pc"), 16),
                               disasm=" ".join(m.group("dis").split()),
                               index=len(steps)))
        if limit and len(steps) >= limit:
            break
    return steps


def _int_auto(m: "re.Match") -> int:
    """Thread id, base 10 unless it is written 0x… — `tid=13` is thirteen."""
    return int(m.group("hart"), 16 if m.group("pfx") else 10)


def parse_rtl_trace(text: str, limit: int = 0) -> list[TraceStep]:
    """A core testbench's own log. Deliberately permissive: any line carrying a
    `pc=` is a retired instruction, so a team doesn't have to adopt a format to
    get value from cosim."""
    steps: list[TraceStep] = []
    for line in text.splitlines():
        m = _RTL_PC.search(line)
        if not m:
            continue
        mi = _RTL_INSN.search(line)
        mh = _RTL_HART.search(line)
        steps.append(TraceStep(pc=int(m.group("pc"), 16),
                               insn=int(mi.group("insn"), 16) if mi else None,
                               hart=_int_auto(mh) if mh else 0,
                               index=len(steps)))
        if limit and len(steps) >= limit:
            break
    return steps


_PARSERS = {"spike": parse_spike_trace, "gdbsim": parse_gdbsim_trace,
            "rtl": parse_rtl_trace}


def parse_trace(text: str, kind: str = "auto", limit: int = 0) -> tuple:
    """(steps, detected_kind). `auto` sniffs the producer."""
    if kind in _PARSERS:
        return _PARSERS[kind](text, limit), kind
    for name in ("spike", "gdbsim", "rtl"):
        steps = _PARSERS[name](text, limit)
        if steps:
            return steps, name
    return [], "unknown"


# ---- the diff ------------------------------------------------------------------------


@dataclass
class Divergence:
    index: int                       # retired-instruction number
    reason: str                      # pc | insn | writeback | length
    dut: TraceStep | None = None
    ref: TraceStep | None = None
    context: list = field(default_factory=list)   # the steps leading in
    pc_offset: int = 0            # added to the core's PC before comparing

    def describe(self) -> str:
        at = f"instruction #{self.index}"
        if self.reason == "length":
            side = "core" if self.dut is None else "reference"
            return (f"{side} stopped after {self.index} instructions while the "
                    f"other kept executing")
        d, r = self.dut, self.ref
        if self.reason == "pc":
            shifted = (f" (+offset 0x{self.pc_offset:x} -> "
                       f"0x{d.pc + self.pc_offset:08x})" if self.pc_offset else "")
            return (f"at {at} the core executed PC 0x{d.pc:08x}{shifted} but the "
                    f"reference executed 0x{r.pc:08x}"
                    + (f" ({r.disasm})" if r.disasm else ""))
        if self.reason == "insn":
            return (f"at {at}, PC 0x{d.pc:08x}: the core fetched "
                    f"0x{(d.insn or 0):08x} but the reference decoded "
                    f"0x{(r.insn or 0):08x}"
                    + (f" ({r.disasm})" if r.disasm else ""))
        return (f"at {at}, PC 0x{d.pc:08x}: the core wrote {d.reg}="
                f"0x{(d.value or 0):x} but the reference wrote {r.reg}="
                f"0x{(r.value or 0):x}"
                + (f" ({r.disasm})" if r.disasm else ""))


def split_by_hart(steps: list[TraceStep]) -> dict:
    """{hart: substream}. Each barrel thread retires its own program order; the
    interleaving between them is a scheduling artifact, not architectural
    behaviour, so it must be removed before comparing with a model."""
    out: dict[int, list[TraceStep]] = {}
    for st in steps:
        out.setdefault(st.hart, []).append(st)
    return out


def align_traces(dut: list[TraceStep], ref: list[TraceStep],
                 window: int = 64, pc_offset: int = 0) -> int:
    """How many LEADING REFERENCE steps to drop so both streams start on the
    same instruction, or 0.

    A model boots before it reaches the program: spike executes a 5-instruction
    bootrom at 0x1000 that jumps to the ELF's entry, which a core never runs.
    Aligning on the core's first PC removes that asymmetry automatically, so
    nobody has to know the bootrom's length."""
    if not dut or not ref:
        return 0
    first = dut[0].pc + pc_offset
    for i, step in enumerate(ref[:window]):
        if step.pc == first:
            return i
    return 0


def diff_traces(dut: list[TraceStep], ref: list[TraceStep], *,
                context: int = 5, skip: int = 0, skip_dut: int | None = None,
                skip_ref: int | None = None, pc_offset: int = 0,
                strict_length: bool = False) -> Divergence | None:
    """First divergence between a core's stream and the reference's, or None.

    Compared strictly in order — the point of lockstep is that the *first*
    difference is the bug; re-syncing afterwards would hide it. `skip` ignores
    leading steps of BOTH streams; `skip_dut`/`skip_ref` override it per side,
    which is what a model bootrom needs (see align_traces) — dropping the same
    count from the core's stream would silently compare nothing.

    `pc_offset` is added to the CORE's PCs before comparing: a core and a model
    often cannot share a memory map (BRISKI boots at 0x0, which spike cannot
    provide because its debug module owns 0x0-0x1000), so the same program is
    built at two bases and compared through the constant difference.

    A stream that simply ENDS while agreeing everywhere it overlapped is a
    prefix match, not a divergence: the reference is usually cut short at a
    step/time bound (bare-metal programs spin forever) and the core's testbench
    stops on its own terms. Set `strict_length` to demand equal lengths."""
    sd = skip if skip_dut is None else skip_dut
    sr = skip if skip_ref is None else skip_ref
    a, b = dut[sd:], ref[sr:]
    for i in range(min(len(a), len(b))):
        d, r = a[i], b[i]
        idx = i + sd
        ctx = [s.__dict__ for s in b[max(0, i - context):i]]
        if d.pc + pc_offset != r.pc:
            return Divergence(idx, "pc", d, r, ctx, pc_offset)
        if d.insn is not None and r.insn is not None and d.insn != r.insn:
            return Divergence(idx, "insn", d, r, ctx, pc_offset)
        if (d.reg and r.reg and d.reg == r.reg
                and d.value is not None and r.value is not None
                and d.value != r.value):
            return Divergence(idx, "writeback", d, r, ctx, pc_offset)
    if strict_length and len(a) != len(b):
        idx = min(len(a), len(b)) + sd
        shorter_is_dut = len(a) < len(b)
        return Divergence(idx, "length",
                          None if shorter_is_dut else a[len(b)],
                          None if not shorter_is_dut else b[len(a)],
                          [s.__dict__ for s in (b if shorter_is_dut else a)
                           [max(0, idx - sd - context):idx - sd]])
    return None
