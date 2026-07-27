"""VCD parser -> indexed in-memory waveform store (SPEC §8.6, FR-WAVE-01).

Parses value-change dump into per-signal change lists keyed by id-code, with the
scope tree preserved so static design facts (domains, drivers) can be joined with
dynamic values. FST is the preferred production format; VCD is the portable one
every open simulator emits, so it is the M0 target. The store is structured for
lazy chunked loading at 10 GB scale, but parses eagerly here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class VcdSignal:
    id_code: str
    name: str
    scope: str  # dotted hierarchical scope
    width: int
    var_type: str = "wire"

    @property
    def path(self) -> str:
        return f"{self.scope}.{self.name}" if self.scope else self.name


@dataclass
class VcdData:
    timescale: str = "1ns"
    end_time: int = 0
    signals: dict[str, VcdSignal] = field(default_factory=dict)  # id_code -> signal
    by_path: dict[str, str] = field(default_factory=dict)  # path -> id_code
    changes: dict[str, list[tuple[int, str]]] = field(default_factory=dict)  # id -> [(t,val)]
    scopes: set[str] = field(default_factory=set)

    def id_for(self, path: str) -> str | None:
        if path in self.by_path:
            return self.by_path[path]
        # tolerate leading testbench scope omissions: match on suffix
        for p, code in self.by_path.items():
            if p.endswith("." + path) or p == path:
                return code
        return None


def _canon_vec(val: str) -> str:
    """Canonical vector value: strip leading zeros (VCD writers differ — fst2vcd
    zero-pads, iverilog doesn't) so `value()` compares stably. X/Z-extended
    values are left untouched (leading x/z is semantic)."""
    if val and val[0] == "0" and all(c in "01" for c in val):
        return val.lstrip("0") or "0"
    return val


_VAR_RE = re.compile(r"\$var\s+(\w+)\s+(\d+)\s+(\S+)\s+(.+?)\s*\$end")
_SCOPE_RE = re.compile(r"\$scope\s+\w+\s+(\S+)\s+\$end")
_TS_RE = re.compile(r"\$timescale\s+(.+?)\s+\$end")


def parse_vcd(path: str) -> VcdData:
    data = VcdData()
    scope_stack: list[str] = []
    in_defs = True
    cur_time = 0
    with open(path, "r", errors="replace") as fh:
        content = fh.read()

    # Header (definitions) can span lines; body is line/token oriented.
    header_end = content.find("$enddefinitions")
    header = content[:header_end] if header_end >= 0 else content
    body = content[header_end:] if header_end >= 0 else ""

    mts = _TS_RE.search(header)
    if mts:
        data.timescale = mts.group(1).strip().replace(" ", "")
    # Walk header tokens for scope/upscope/var
    for m in re.finditer(r"\$scope\s+\w+\s+(\S+)\s+\$end|\$upscope\s+\$end|"
                         r"\$var\s+(\w+)\s+(\d+)\s+(\S+)\s+(.+?)\s*\$end", header):
        if m.group(0).startswith("$scope"):
            scope_stack.append(m.group(1))
            data.scopes.add(".".join(scope_stack))
        elif m.group(0).startswith("$upscope"):
            if scope_stack:
                scope_stack.pop()
        else:
            var_type, width, code, name = m.group(2), int(m.group(3)), m.group(4), m.group(5)
            name = name.strip()
            # strip bit-select suffix in name like "data [7:0]"
            mname = re.match(r"([^\s\[]+)\s*(\[.*\])?", name)
            base = mname.group(1) if mname else name
            scope = ".".join(scope_stack)
            sig = VcdSignal(id_code=code, name=base, scope=scope, width=width,
                            var_type=var_type)
            if code not in data.signals:
                data.signals[code] = sig
                data.changes.setdefault(code, [])
            data.by_path[sig.path] = code

    # Body: value changes
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("$"):
            continue
        c0 = line[0]
        if c0 == "#":
            try:
                cur_time = int(line[1:])
                data.end_time = max(data.end_time, cur_time)
            except ValueError:
                pass
        elif c0 in "01xXzZ":
            val, code = line[0], line[1:]
            if code in data.changes:
                data.changes[code].append((cur_time, val.lower()))
        elif c0 in "bB":
            m = re.match(r"[bB](\S+)\s+(\S+)", line)
            if m:
                val, code = _canon_vec(m.group(1).lower()), m.group(2)
                if code in data.changes:
                    data.changes[code].append((cur_time, val))
        elif c0 in "rR":
            m = re.match(r"[rR](\S+)\s+(\S+)", line)
            if m and m.group(2) in data.changes:
                data.changes[m.group(2)].append((cur_time, m.group(1)))
    return data
