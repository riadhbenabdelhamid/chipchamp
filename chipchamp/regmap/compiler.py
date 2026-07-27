"""Built-in register-map compiler (SPEC §9-I, playbook P10, US-09).

Compiles a YAML register spec (OpenTitan-regtool-flavored) into:
  - an APB slave register block (SystemVerilog),
  - a C header,
  - a markdown register table.

Output is **deterministic** (no timestamps, stable ordering), which is what makes
the ``regmap_regen`` gate possible: regenerate and byte-compare proves the
committed outputs match the source of truth (FR-PROJ-04 / FR-DB-05 — you edit the
spec, never the generated RTL).

Field access types: ``RW`` (sw writes, hw observes), ``RO`` (hw drives, sw
reads), ``W1C`` (hw sets sticky bit, sw writes 1 to clear; hw set wins on
conflict). Registers are 32-bit; APB with always-ready completion and ``pslverr``
on unmapped addresses.
"""
from __future__ import annotations

def _brand() -> str:
    from ..brand import APP_NAME
    return APP_NAME


import re
from dataclasses import dataclass, field

import yaml

REG_WIDTH = 32
_ACCESS = {"RW", "RO", "W1C"}


@dataclass
class Field:
    name: str
    msb: int
    lsb: int
    access: str
    reset: int = 0
    desc: str = ""

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1

    @property
    def slice(self) -> str:
        return f"[{self.msb}:{self.lsb}]" if self.msb != self.lsb else f"[{self.lsb}]"


@dataclass
class Register:
    name: str
    offset: int
    desc: str = ""
    fields: list[Field] = field(default_factory=list)


@dataclass
class RegmapSpec:
    name: str
    bus: str = "apb"
    addr_width: int = 12
    registers: list[Register] = field(default_factory=list)


def _parse_bits(bits) -> tuple[int, int]:
    s = str(bits)
    m = re.fullmatch(r"(\d+)\s*:\s*(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return int(s), int(s)


def load_spec(path: str) -> RegmapSpec:
    data = yaml.safe_load(open(path))
    spec = RegmapSpec(name=data["name"], bus=data.get("bus", "apb"),
                      addr_width=int(data.get("addr_width", 12)))
    for rd in data.get("registers", []):
        reg = Register(name=rd["name"], offset=int(rd["offset"]),
                       desc=rd.get("desc", ""))
        for fd in rd.get("fields", []):
            msb, lsb = _parse_bits(fd["bits"])
            reg.fields.append(Field(
                name=fd["name"], msb=msb, lsb=lsb,
                access=str(fd.get("access", "RW")).upper(),
                reset=int(fd.get("reset", 0)), desc=fd.get("desc", "")))
        spec.registers.append(reg)
    return spec


def validate(spec: RegmapSpec) -> list[str]:
    errors: list[str] = []
    if spec.bus != "apb":
        errors.append(f"unsupported bus '{spec.bus}' (builtin compiler: apb)")
    seen_names: set[str] = set()
    seen_offsets: set[int] = set()
    for reg in spec.registers:
        if reg.name.lower() in seen_names:
            errors.append(f"duplicate register name {reg.name}")
        seen_names.add(reg.name.lower())
        if reg.offset % 4:
            errors.append(f"{reg.name}: offset 0x{reg.offset:x} not word-aligned")
        if reg.offset in seen_offsets:
            errors.append(f"{reg.name}: offset 0x{reg.offset:x} already used")
        seen_offsets.add(reg.offset)
        if reg.offset >= (1 << spec.addr_width):
            errors.append(f"{reg.name}: offset beyond addr_width")
        used = 0
        fnames: set[str] = set()
        for f in reg.fields:
            if f.name.lower() in fnames:
                errors.append(f"{reg.name}.{f.name}: duplicate field name")
            fnames.add(f.name.lower())
            if f.access not in _ACCESS:
                errors.append(f"{reg.name}.{f.name}: access '{f.access}' not in {sorted(_ACCESS)}")
            if not (0 <= f.lsb <= f.msb < REG_WIDTH):
                errors.append(f"{reg.name}.{f.name}: bits {f.msb}:{f.lsb} out of range")
                continue
            mask = ((1 << f.width) - 1) << f.lsb
            if used & mask:
                errors.append(f"{reg.name}.{f.name}: overlaps another field")
            used |= mask
            if f.reset >> f.width:
                errors.append(f"{reg.name}.{f.name}: reset value wider than field")
    return errors


# ---- generation --------------------------------------------------------------


def generate(spec: RegmapSpec) -> dict[str, str]:
    """Return {relative_filename: content}. Deterministic."""
    return {
        f"{spec.name}_reg_top.sv": _gen_sv(spec),
        f"{spec.name}_regs.h": _gen_header(spec),
        f"{spec.name}_regs.md": _gen_md(spec),
    }


def _q(reg: Register, f: Field) -> str:
    return f"{reg.name.lower()}_{f.name.lower()}_q"


def _hwif(spec_reg: Register, f: Field) -> str:
    base = f"hwif_{spec_reg.name.lower()}_{f.name.lower()}"
    return {"RW": base + "_o", "RO": base + "_i", "W1C": base + "_set_i"}[f.access]


def _rng(f: Field) -> str:
    return f"[{f.width - 1}:0] " if f.width > 1 else ""


def _gen_sv(spec: RegmapSpec) -> str:
    aw = spec.addr_width
    word_hi = aw - 1
    L: list[str] = []
    L.append(f"// Generated by {_brand()} regmap from spec '{spec.name}'. DO NOT EDIT.")
    L.append(f"// Edit the register spec and regenerate (SPEC FR-PROJ-04).")
    L.append(f"module {spec.name}_reg_top (")
    ports = [
        "  input  logic        pclk",
        "  input  logic        presetn",
        "  input  logic        psel",
        "  input  logic        penable",
        "  input  logic        pwrite",
        f"  input  logic [{word_hi}:0] paddr",
        "  input  logic [31:0] pwdata",
        "  output logic [31:0] prdata",
        "  output logic        pready",
        "  output logic        pslverr",
    ]
    for reg in spec.registers:
        for f in reg.fields:
            direction = "output" if f.access == "RW" else "input "
            ports.append(f"  {direction} logic {_rng(f)}{_hwif(reg, f)}")
    L.append(",\n".join(ports))
    L.append(");")
    L.append("")
    # storage
    for reg in spec.registers:
        for f in reg.fields:
            if f.access in ("RW", "W1C"):
                L.append(f"  logic {_rng(f)}{_q(reg, f)};")
    L.append("")
    L.append("  wire apb_write = psel & penable & pwrite;")
    L.append("  assign pready = 1'b1;")
    L.append("")
    # select decode
    sels = []
    for reg in spec.registers:
        sel = f"sel_{reg.name.lower()}"
        sels.append(sel)
        L.append(f"  wire {sel} = (paddr[{word_hi}:2] == {aw - 2}'d{reg.offset >> 2});")
    L.append(f"  wire sel_any = " + " | ".join(sels) + ";")
    L.append("  assign pslverr = psel & penable & ~sel_any;")
    L.append("")
    # field logic
    for reg in spec.registers:
        sel = f"sel_{reg.name.lower()}"
        rw = [f for f in reg.fields if f.access == "RW"]
        w1c = [f for f in reg.fields if f.access == "W1C"]
        if rw:
            L.append(f"  // {reg.name} (RW fields)")
            L.append("  always_ff @(posedge pclk or negedge presetn) begin")
            L.append("    if (!presetn) begin")
            for f in rw:
                L.append(f"      {_q(reg, f)} <= {f.width}'h{f.reset:x};")
            L.append(f"    end else if (apb_write & {sel}) begin")
            for f in rw:
                L.append(f"      {_q(reg, f)} <= pwdata{f.slice};")
            L.append("    end")
            L.append("  end")
        for f in w1c:
            L.append(f"  // {reg.name}.{f.name} (W1C: hw set wins over sw clear)")
            L.append("  always_ff @(posedge pclk or negedge presetn) begin")
            L.append(f"    if (!presetn) {_q(reg, f)} <= {f.width}'h{f.reset:x};")
            L.append(f"    else if ({_hwif(reg, f)}) {_q(reg, f)} <= {{{f.width}{{1'b1}}}};")
            L.append(f"    else if (apb_write & {sel} & (|pwdata{f.slice})) "
                     f"{_q(reg, f)} <= '0;")
            L.append("  end")
        for f in rw:
            L.append(f"  assign {_hwif(reg, f)} = {_q(reg, f)};")
        L.append("")
    # read mux
    L.append("  always_comb begin")
    L.append("    prdata = 32'h0;")
    for reg in spec.registers:
        L.append(f"    if (sel_{reg.name.lower()}) begin")
        for f in reg.fields:
            src = {"RW": _q(reg, f), "W1C": _q(reg, f),
                   "RO": _hwif(reg, f)}[f.access]
            L.append(f"      prdata{f.slice} = {src};")
        L.append("    end")
    L.append("  end")
    L.append("")
    L.append("endmodule")
    L.append("")
    return "\n".join(L)


def _gen_header(spec: RegmapSpec) -> str:
    guard = f"{spec.name.upper()}_REGS_H_"
    L = [f"// Generated by {_brand()} regmap from spec '{spec.name}'. DO NOT EDIT.",
         f"#ifndef {guard}", f"#define {guard}", ""]
    for reg in spec.registers:
        L.append(f"#define {spec.name.upper()}_{reg.name.upper()}_OFFSET 0x{reg.offset:x}")
        for f in reg.fields:
            base = f"{spec.name.upper()}_{reg.name.upper()}_{f.name.upper()}"
            mask = ((1 << f.width) - 1) << f.lsb
            L.append(f"#define {base}_LSB {f.lsb}")
            L.append(f"#define {base}_MASK 0x{mask:x}")
        L.append("")
    L.append(f"#endif  // {guard}")
    L.append("")
    return "\n".join(L)


def _gen_md(spec: RegmapSpec) -> str:
    L = [f"# {spec.name} register map",
         "",
         f"_Generated from the register spec — do not edit. Bus: {spec.bus.upper()}, "
         f"address width: {spec.addr_width}._",
         ""]
    for reg in spec.registers:
        L.append(f"## {reg.name} @ 0x{reg.offset:03x}")
        if reg.desc:
            L.append(f"{reg.desc}")
        L.append("")
        L.append("| bits | field | access | reset | description |")
        L.append("|---|---|---|---|---|")
        for f in sorted(reg.fields, key=lambda x: -x.msb):
            bits = f"{f.msb}:{f.lsb}" if f.msb != f.lsb else str(f.lsb)
            L.append(f"| {bits} | {f.name} | {f.access} | 0x{f.reset:x} | {f.desc} |")
        L.append("")
    return "\n".join(L)
