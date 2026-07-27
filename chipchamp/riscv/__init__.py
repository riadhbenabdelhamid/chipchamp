"""RISC-V vertical (SPEC §8.4): software toolchain + ISA-level co-simulation.

A RISC-V core is only a RISC-V core if it executes the ISA the way the ISA says.
That claim is checkable mechanically, and this package supplies the two halves:
build real programs (:mod:`chipchamp.riscv.toolchain`) and compare the core's
retired-instruction stream against a reference model
(:mod:`chipchamp.riscv.trace`), reporting the FIRST divergence with its PC and
disassembly rather than "test 7 hangs".
"""
from .toolchain import (Toolchain, discover_toolchain, elf_to_hex, elf_sections,
                        elf_symbols)
from .trace import (TraceStep, diff_traces, parse_gdbsim_trace, parse_rtl_trace)

__all__ = ["Toolchain", "discover_toolchain", "elf_to_hex", "elf_sections",
           "elf_symbols", "TraceStep", "diff_traces", "parse_gdbsim_trace",
           "parse_rtl_trace"]
