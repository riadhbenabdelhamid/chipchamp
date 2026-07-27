"""Gate-netlist → RTL cross-probe.

Post-synthesis instance names (``_54_``, or ``u_core.u_alu._88_`` after
flattening) are meaningless to a designer; the nets on flop pins still carry
RTL names (``count[7]``, ``\\u_core.pc[3]``). This module parses the gate-level
netlist, recovers the RTL net behind a timing start/endpoint, and resolves it
through the design database to (module, signal, file, line).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# "sky130_fd_sc_hd__dfrtp_2 _54_ (.CLK(x), .D(_07_), .Q(count[7]));" — also
# escaped identifiers: "\u_core.pc[7] " (backslash..space) and hierarchical
# instance names after flatten.
_INST = re.compile(
    r"^\s*(?P<cell>[A-Za-z_$\\][\w$.\\\[\]]*)\s+(?P<inst>\\?\S+)\s*\(",
    re.MULTILINE)
_PIN = re.compile(r"\.(?P<pin>\w+)\s*\(\s*(?P<net>[^()]*?)\s*\)")

# sequential cells across sky130/gf180/generic yosys/liberty naming
_SEQ_CELL = re.compile(r"df|dlx|latch|sdff|_ff_|DFF|LATCH", re.IGNORECASE)


@dataclass
class GateInst:
    name: str
    cell: str
    pins: dict[str, str] = field(default_factory=dict)

    @property
    def is_seq(self) -> bool:
        return bool(_SEQ_CELL.search(self.cell))


@dataclass
class GateNetlist:
    insts: dict[str, GateInst] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "GateNetlist":
        nl = cls()
        for m in _INST.finditer(text):
            cell, inst = m["cell"], m["inst"]
            if cell in ("module", "input", "output", "inout", "wire", "assign"):
                continue
            # capture the connection list up to the closing ");"
            end = text.find(");", m.end())
            conns = text[m.end():end if end >= 0 else m.end()]
            pins = {p["pin"]: _clean(p["net"]) for p in _PIN.finditer(conns)}
            nl.insts[_clean(inst)] = GateInst(name=_clean(inst), cell=cell,
                                              pins=pins)
        return nl

    @classmethod
    def load(cls, path: str) -> "GateNetlist":
        with open(path, "r", errors="replace") as fh:
            return cls.parse(fh.read())


def _clean(name: str) -> str:
    return name.strip().lstrip("\\").strip()


def rtl_net_of(point: str, netlist: GateNetlist) -> str | None:
    """RTL-meaningful net behind a timing start/endpoint.

    ``point`` is an instance or ``inst/pin`` from an STA report. Ports come
    back unchanged; flops prefer their Q (state name), then D; other cells the
    driven output net. Anonymous nets (``_07_``, ``net13``) return None."""
    inst_name = _clean(point.split("/")[0])
    inst = netlist.insts.get(inst_name)
    if inst is None:
        return point if not re.fullmatch(r"_\d+_|net\d+", _clean(point)) else None
    order = ("Q", "Q_N", "D", "X", "Y", "Z") if inst.is_seq else ("X", "Y", "Z", "Q")
    for pin in order:
        net = inst.pins.get(pin)
        if net and not re.fullmatch(r"_\d+_|net\d+|1'[bh][01x]", net):
            return net
    return None


def resolve_hier(db, top: str, netname: str) -> dict | None:
    """(module, signal, file, line) for a hierarchical RTL net name.

    ``u_core.u_alu.acc[3]`` walks instance names from ``top`` through the
    elaborated hierarchy; the un-consumed tail is the signal. Flat names
    resolve in ``top`` itself."""
    if not netname:
        return None
    base = re.sub(r"\[\d+(?::\d+)?\]$", "", netname)  # strip bit select
    parts = base.split(".")
    node = db.elaborate(top)
    if node is None:
        return None
    i = 0
    while i < len(parts) - 1:
        nxt = next((c for c in node.children if c.inst_name == parts[i]), None)
        if nxt is None:
            break
        node = nxt
        i += 1
    signal = ".".join(parts[i:])
    mod = db.module(node.module)
    if mod is None:
        return None
    out = {"module": node.module, "instance_path": node.path,
           "signal": signal, "file": mod.file, "line": mod.line}
    # tighten line to the signal's declaration/first drive when findable
    try:
        with open(mod.file, "r", errors="replace") as fh:
            for ln, text in enumerate(fh, 1):
                if re.search(rf"\b{re.escape(signal)}\b", text):
                    out["line"] = ln
                    break
    except OSError:
        pass
    return out
