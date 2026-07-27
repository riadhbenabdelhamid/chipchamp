"""Pin constraints: parse, validate and generate — for boards, not just parts.

The FPGA flow's most expensive failure is a pin problem found late: an XDC typo
surfaces after synthesis, an unconstrained port only at ``write_bitstream``
(Vivado's UCIO-1/NSTD-1). Everything needed to catch those in under a second is
already on hand — the design DB knows the top-level ports (name, direction,
msb/lsb), and a constraints file's common form is declarative text.

Three formats, one shape::

    xdc (Vivado)  set_property -dict {PACKAGE_PIN E3 IOSTANDARD LVCMOS33} [get_ports clk]
    pcf (ice40)   set_io clk 35
    lpf (ecp5)    LOCATE COMP "clk" SITE "G2";  IOBUF PORT "clk" IO_TYPE=LVCMOS33;

**Boards.** A part tells you which pins exist; a *board* tells you what they are
wired to. Board profiles carry ``signal -> (pin, iostandard)`` and come from
authoritative sources only — Vivado's installed Digilent ``board_files``
(``part0_pins.xml``), or a vendor master constraints file the user imports.
Nothing here hardcodes a pinout from memory: a wrong pin can short an output
and damage hardware, so an unmapped port is reported, never guessed.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field

# ---- design ports -> flat bits ---------------------------------------------------


def _as_int(v):
    """The design DB carries bounds as source text ('7', or 'WIDTH-1' when
    parameterized). Only a literal expands; anything symbolic stays unknown."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def expand_ports(module) -> list[str]:
    """Top-level ports as the flat bit names a constraints file uses:
    ``count`` [7:0] -> count[0]..count[7]; a scalar stays bare. A vector whose
    bounds are parameterized can't be expanded — it stays bare rather than
    inventing a width."""
    out: list[str] = []
    for p in getattr(module, "ports", []):
        msb, lsb = _as_int(getattr(p, "msb", None)), _as_int(getattr(p, "lsb", None))
        if msb is not None and lsb is not None:
            lo, hi = min(msb, lsb), max(msb, lsb)
            out.extend(f"{p.name}[{i}]" for i in range(lo, hi + 1))
        else:
            out.append(p.name)
    return out


def _norm(name: str) -> str:
    """``count[3]``/``count(3)``/``count_3`` -> ``count[3]`` so the three
    formats' spellings compare equal."""
    name = name.strip().strip('"')
    m = re.fullmatch(r"(.+?)[\[\(](\d+)[\]\)]", name)
    return f"{m.group(1)}[{m.group(2)}]" if m else name


# ---- constraint file parsing -------------------------------------------------------


@dataclass
class PinConstraint:
    port: str                  # normalized bit name
    pin: str = ""
    iostandard: str = ""
    line: int = 0


# `[get_ports {count[0]}]` or `[get_ports clk]`. The braced form must be matched
# as a unit: a bus index contains `]`, so a character class excluding `]` would
# silently truncate `count[0]` to `count[0` and report every bus bit as both
# unknown and unconstrained.
_PORTS = (r"\[\s*get_ports\s*(?:\{\s*(?P<pb>[^}]+?)\s*\}|(?P<pn>[^\s\]]+))\s*\]")
# `set_property -dict {PACKAGE_PIN E3 IOSTANDARD LVCMOS33} [get_ports {clk}]`
_XDC_DICT = re.compile(
    r"set_property\s+-dict\s*\{(?P<body>[^}]*)\}\s*" + _PORTS, re.I)
# `set_property PACKAGE_PIN E3 [get_ports clk]`
_XDC_ONE = re.compile(
    r"set_property\s+(?P<key>PACKAGE_PIN|IOSTANDARD)\s+(?P<val>\S+)\s*"
    + _PORTS, re.I)


def _port_of(m: "re.Match") -> str:
    return (m.group("pb") or m.group("pn") or "").strip()


def _is_literal_port(name: str) -> bool:
    """A real port name, not a Tcl expression. `count[$i]` from a foreach, or a
    `led*` wildcard, must be reported as UNPARSED — claiming it is an unknown
    port would be a false error on a perfectly valid file."""
    if not name or any(ch in name for ch in "$*?\\"):
        return False
    return name.count("[") == name.count("]")
_XDC_KV = re.compile(r"(PACKAGE_PIN|IOSTANDARD)\s+(\S+)", re.I)
_PCF = re.compile(r"^\s*set_io\s+(?:--warn-no-port\s+)?(?P<port>\S+)\s+(?P<pin>\S+)", re.I)
_LPF_LOC = re.compile(r'LOCATE\s+COMP\s+"(?P<port>[^"]+)"\s+SITE\s+"(?P<pin>[^"]+)"', re.I)
_LPF_IO = re.compile(r'IOBUF\s+PORT\s+"(?P<port>[^"]+)"\s+.*?IO_TYPE\s*=\s*(?P<std>\w+)', re.I)


def detect_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext if ext in ("xdc", "pcf", "lpf") else "xdc"


def parse_constraints(text: str, fmt: str) -> tuple[dict, list[int]]:
    """(``{port: PinConstraint}``, unparsed line numbers).

    Only the declarative subset is understood — XDC is Tcl, so loops,
    wildcards and computed `get_ports` are reported as unparsed rather than
    silently ignored, and callers must say so."""
    found: dict[str, PinConstraint] = {}
    unparsed: list[int] = []

    def slot(port: str, ln: int) -> PinConstraint:
        key = _norm(port)
        if key not in found:
            found[key] = PinConstraint(port=key, line=ln)
        return found[key]

    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#")[0] if fmt == "pcf" else raw
        line = re.sub(r"//.*$", "", line).strip()
        if fmt == "xdc":
            line = re.sub(r"^\s*#.*$", "", line).strip()
        if not line:
            continue
        hit = False
        if fmt == "xdc":
            for m in _XDC_DICT.finditer(line):
                port = _port_of(m)
                if not _is_literal_port(port):
                    unparsed.append(i)
                    hit = True
                    continue
                c = slot(port, i)
                for k, v in _XDC_KV.findall(m.group("body")):
                    if k.upper() == "PACKAGE_PIN":
                        c.pin = v
                    else:
                        c.iostandard = v
                hit = True
            for m in _XDC_ONE.finditer(line):
                port = _port_of(m)
                if not _is_literal_port(port):
                    unparsed.append(i)
                    hit = True
                    continue
                c = slot(port, i)
                if m.group("key").upper() == "PACKAGE_PIN":
                    c.pin = m.group("val")
                else:
                    c.iostandard = m.group("val")
                hit = True
            if not hit and "get_ports" in line:
                unparsed.append(i)          # Tcl we don't model
        elif fmt == "pcf":
            m = _PCF.search(line)
            if m:
                slot(m.group("port"), i).pin = m.group("pin")
                hit = True
            elif line.startswith("set_"):
                unparsed.append(i)
        else:  # lpf
            m = _LPF_LOC.search(line)
            if m:
                slot(m.group("port"), i).pin = m.group("pin")
                hit = True
            m2 = _LPF_IO.search(line)
            if m2:
                slot(m2.group("port"), i).iostandard = m2.group("std")
                hit = True
            if not hit and re.match(r"^\s*(LOCATE|IOBUF)\b", line, re.I):
                unparsed.append(i)
    return found, unparsed


# ---- emitting ----------------------------------------------------------------------


def emit_constraints(rows: list[PinConstraint], fmt: str, *, header: str = "",
                     todo: list[str] | None = None) -> str:
    """Render constraints. `todo` ports are emitted as commented placeholders —
    an unmapped port must never silently receive a made-up pin."""
    out: list[str] = []
    if header:
        out.extend(f"# {ln}" for ln in header.splitlines())
        out.append("")
    for c in rows:
        if fmt == "xdc":
            std = f" IOSTANDARD {c.iostandard}" if c.iostandard else ""
            out.append(f"set_property -dict {{PACKAGE_PIN {c.pin}{std}}} "
                       f"[get_ports {{{c.port}}}]")
        elif fmt == "pcf":
            out.append(f"set_io {c.port} {c.pin}")
        else:
            out.append(f'LOCATE COMP "{c.port}" SITE "{c.pin}";')
            if c.iostandard:
                out.append(f'IOBUF PORT "{c.port}" IO_TYPE={c.iostandard};')
    for port in (todo or []):
        out.append(f"# TODO unmapped: {port} — no board signal matched; "
                   f"assign a pin from the board's pinout")
    return "\n".join(out) + "\n"


# ---- board profiles ----------------------------------------------------------------


@dataclass
class Board:
    name: str
    part: str = ""
    fmt: str = "xdc"
    source: str = ""
    signals: dict = field(default_factory=dict)  # signal -> {pin, iostandard}

    def to_json(self) -> dict:
        return {"name": self.name, "part": self.part, "format": self.fmt,
                "source": self.source, "signals": self.signals}


def _vivado_board_dirs() -> list[str]:
    roots = []
    env = os.environ.get("XILINX_VIVADO", "")
    if env:
        roots.append(os.path.join(env, "data", "boards", "board_files"))
    roots.extend(sorted(glob.glob(os.path.expanduser(
        "~/Xilinx/*/Vivado/data/boards/board_files"))))
    return [r for r in roots if os.path.isdir(r)]


_XML_PIN = re.compile(
    r'<pin\s+index\s*=\s*"[^"]*"\s+name\s*=\s*"(?P<name>[^"]+)"\s+'
    r'iostandard\s*=\s*"(?P<std>[^"]*)"\s+loc\s*=\s*"(?P<loc>[^"]+)"', re.I)
_XML_PART = re.compile(r'part_name\s*=\s*"([^"]+)"', re.I)


def _load_vivado_board(pins_xml: str, root: str = "") -> Board | None:
    """A Digilent board_files entry: part0_pins.xml (signal→pin+iostandard)
    plus board.xml for the part. Authoritative, shipped with Vivado."""
    try:
        with open(pins_xml, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    signals = {m.group("name"): {"pin": m.group("loc"),
                                 "iostandard": m.group("std") or "LVCMOS33"}
               for m in _XML_PIN.finditer(text)}
    if not signals:
        return None
    part = ""
    board_xml = os.path.join(os.path.dirname(pins_xml), "board.xml")
    try:
        with open(board_xml, "r", errors="replace") as fh:
            m = _XML_PART.search(fh.read())
            part = m.group(1) if m else ""
    except OSError:
        pass
    # .../board_files/<name>/<rev>[/<ver>]/part0_pins.xml — the revision depth
    # varies per board, so take the first segment under the root.
    name = ""
    if root:
        rel = os.path.relpath(os.path.abspath(pins_xml), os.path.abspath(root))
        name = rel.split(os.sep)[0]
    if not name or name.startswith(".."):
        name = os.path.basename(os.path.dirname(os.path.dirname(pins_xml)))
    return Board(name=name, part=part, fmt="xdc", source="vivado",
                 signals=signals)


def boards_dir(ws) -> str:
    return os.path.join(str(ws.dot), "boards")


def discover_boards(ws=None) -> dict:
    """Every known board: Vivado's installed board files, then user profiles in
    ``.chipchamp/boards/*.json`` (which win on a name clash)."""
    out: dict[str, Board] = {}
    for root in _vivado_board_dirs():
        # board revision/version nesting varies (…/<name>/<rev>/part0_pins.xml
        # and …/<name>/<rev>/<ver>/…), so recurse instead of fixing the depth.
        for pins_xml in sorted(glob.glob(
                os.path.join(root, "**", "part0_pins.xml"), recursive=True)):
            b = _load_vivado_board(pins_xml, root)
            if b and b.name not in out:
                out[b.name] = b
    if ws is not None:
        for path in sorted(glob.glob(os.path.join(boards_dir(ws), "*.json"))):
            try:
                with open(path, "r", errors="replace") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            name = str(d.get("name") or os.path.splitext(os.path.basename(path))[0])
            out[name] = Board(name=name, part=str(d.get("part", "")),
                              fmt=str(d.get("format", "xdc")),
                              source="user", signals=d.get("signals", {}) or {})
    return out


def board_from_constraints(name: str, text: str, fmt: str, part: str = "") -> Board:
    """Import a vendor master constraints file as a board profile — the
    accurate way to add a board chipchamp doesn't ship."""
    parsed, _ = parse_constraints(text, fmt)
    signals = {c.port: {"pin": c.pin, "iostandard": c.iostandard}
               for c in parsed.values() if c.pin}
    return Board(name=name, part=part, fmt=fmt, source="import",
                 signals=signals)


# ---- mapping design ports onto board signals ---------------------------------------

def _candidates(base: str, idx) -> list[str]:
    if idx is None:
        return [base]
    return [f"{base}[{idx}]", f"{base}_{idx}", f"{base}{idx}"]


def map_ports(bits: list[str], board: Board, mapping: dict | None = None) -> tuple:
    """(rows, unmapped). A port maps to a board signal by explicit `mapping`
    (bus base names allowed) or an exact name match — never by guessing a free
    pin, which is how you damage hardware."""
    mapping = {k.strip(): str(v).strip() for k, v in (mapping or {}).items()}
    rows: list[PinConstraint] = []
    unmapped: list[str] = []
    for bit in bits:
        m = re.fullmatch(r"(.+?)\[(\d+)\]", bit)
        base, idx = (m.group(1), int(m.group(2))) if m else (bit, None)
        targets: list[str] = []
        if bit in mapping:                       # exact bit override
            targets = [mapping[bit]]
        elif base in mapping:                    # bus base -> board bus base
            targets = _candidates(mapping[base], idx)
        else:
            targets = _candidates(base, idx) if idx is not None else [base]
        sig = next((t for t in targets if t in board.signals), "")
        if not sig:
            unmapped.append(bit)
            continue
        info = board.signals[sig]
        rows.append(PinConstraint(port=bit, pin=info.get("pin", ""),
                                  iostandard=info.get("iostandard", "")))
    return rows, unmapped


# ---- validation ---------------------------------------------------------------------


def validate(bits: list[str], parsed: dict, *, board: Board | None = None,
             fmt: str = "xdc", unparsed: list[int] | None = None) -> list[dict]:
    """Everything checkable without running a tool. Each finding is
    {severity, kind, port/pin, message}."""
    findings: list[dict] = []
    design = set(bits)
    for port in sorted(parsed):
        if port not in design:
            findings.append({
                "severity": "error", "kind": "unknown_port", "port": port,
                "message": f"'{port}' is constrained but is not a top-level "
                           f"port of the design (typo, or stale constraint)"})
    for bit in bits:
        if bit not in parsed:
            findings.append({
                "severity": "error", "kind": "unconstrained", "port": bit,
                "message": f"'{bit}' has no pin constraint — Vivado's UCIO-1 "
                           f"will block bitstream generation"})
    if fmt in ("xdc", "lpf"):
        for port, c in sorted(parsed.items()):
            if port in design and not c.iostandard:
                findings.append({
                    "severity": "error", "kind": "missing_iostandard",
                    "port": port,
                    "message": f"'{port}' has no IOSTANDARD — Vivado's NSTD-1 "
                               f"will block bitstream generation"})
    seen: dict[str, str] = {}
    for port, c in sorted(parsed.items()):
        if not c.pin:
            continue
        if c.pin in seen:
            findings.append({
                "severity": "error", "kind": "duplicate_pin", "pin": c.pin,
                "port": port,
                "message": f"pin {c.pin} is assigned to both '{seen[c.pin]}' "
                           f"and '{port}'"})
        else:
            seen[c.pin] = port
    if board is not None and board.signals:
        legal = {i.get("pin") for i in board.signals.values()}
        for port, c in sorted(parsed.items()):
            if c.pin and c.pin not in legal:
                findings.append({
                    "severity": "warning", "kind": "pin_not_on_board",
                    "port": port, "pin": c.pin,
                    "message": f"pin {c.pin} for '{port}' is not a pin the "
                               f"{board.name} profile knows — check the board's "
                               f"pinout"})
    for ln in (unparsed or []):
        findings.append({
            "severity": "warning", "kind": "unparsed", "line": ln,
            "message": f"line {ln} is not the declarative form chipchamp "
                       f"understands (constraints files are Tcl) — it was NOT "
                       f"checked"})
    return findings
