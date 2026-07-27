"""Pin constraints (SPEC §8.4 FPGA vertical): parsing, validation, board
profiles and generation. All offline — board data is faked into a temp tree so
the suite doesn't need Vivado installed."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace as NS

from chipchamp.physical.pins import (Board, board_from_constraints,
                                     detect_format, discover_boards,
                                     emit_constraints, expand_ports, map_ports,
                                     parse_constraints, validate)


def _mod(*ports):
    return NS(name="counter", ports=[NS(name=n, msb=m, lsb=l, direction=d)
                                     for n, m, l, d in ports])


COUNTER = _mod(("clk", None, None, "input"), ("rst_n", None, None, "input"),
               ("en", None, None, "input"), ("clr", None, None, "input"),
               ("count", "7", "0", "output"), ("tc", None, None, "output"))


# ---- port expansion --------------------------------------------------------------


def test_expand_ports_handles_string_bounds():
    """The design DB carries bounds as SOURCE TEXT ('7'), not ints — treating
    them as ints-only silently skipped every bus."""
    bits = expand_ports(COUNTER)
    assert len(bits) == 13
    assert "count[0]" in bits and "count[7]" in bits
    assert "clk" in bits and "count" not in bits


def test_expand_ports_leaves_parameterized_widths_alone():
    m = _mod(("data", "WIDTH-1", "0", "input"))
    assert expand_ports(m) == ["data"]      # no invented width


# ---- xdc parsing -------------------------------------------------------------------


def test_xdc_parses_bus_indices():
    """Regression: a port class excluding ']' truncated `count[0]` to `count[0`,
    making every bus bit read as unknown AND unconstrained."""
    text = ("set_property -dict {PACKAGE_PIN B15 IOSTANDARD LVCMOS33} "
            "[get_ports {count[0]}]\n"
            "set_property -dict {PACKAGE_PIN A16 IOSTANDARD LVCMOS33} [get_ports clk]\n")
    parsed, unparsed = parse_constraints(text, "xdc")
    assert set(parsed) == {"count[0]", "clk"}
    assert parsed["count[0]"].pin == "B15"
    assert parsed["count[0]"].iostandard == "LVCMOS33"
    assert not unparsed


def test_xdc_parses_separate_set_property_form():
    text = ("set_property PACKAGE_PIN E3 [get_ports clk]\n"
            "set_property IOSTANDARD LVCMOS33 [get_ports clk]\n")
    parsed, _ = parse_constraints(text, "xdc")
    assert parsed["clk"].pin == "E3" and parsed["clk"].iostandard == "LVCMOS33"


def test_tcl_is_reported_unparsed_not_as_a_bogus_port():
    """Constraints files are Tcl. A foreach loop must be declared un-checked,
    never turned into an 'unknown port' false error."""
    text = "foreach i {1 2 3} { set_property PACKAGE_PIN X [get_ports count[$i]] }\n"
    parsed, unparsed = parse_constraints(text, "xdc")
    assert parsed == {}
    assert unparsed == [1]
    found = validate(expand_ports(COUNTER), parsed, unparsed=unparsed)
    assert not [f for f in found if f["kind"] == "unknown_port"]
    assert [f for f in found if f["kind"] == "unparsed"][0]["severity"] == "warning"


# ---- pcf / lpf ---------------------------------------------------------------------


def test_pcf_and_lpf_roundtrip():
    pcf, _ = parse_constraints("set_io clk 35\nset_io count[0] 2\n# comment\n", "pcf")
    assert pcf["clk"].pin == "35" and pcf["count[0]"].pin == "2"
    lpf, _ = parse_constraints(
        'LOCATE COMP "clk" SITE "G2";\nIOBUF PORT "clk" IO_TYPE=LVCMOS33;\n', "lpf")
    assert lpf["clk"].pin == "G2" and lpf["clk"].iostandard == "LVCMOS33"
    rows = list(pcf.values())
    assert "set_io clk 35" in emit_constraints(rows, "pcf")
    assert 'LOCATE COMP "clk" SITE "35";' in emit_constraints(rows, "lpf")
    assert "PACKAGE_PIN 35" in emit_constraints(rows, "xdc")


def test_detect_format():
    assert detect_format("a/b.pcf") == "pcf" and detect_format("x.LPF") == "lpf"
    assert detect_format("pins.xdc") == "xdc" and detect_format("noext") == "xdc"


# ---- validation ---------------------------------------------------------------------


def test_validate_catches_the_real_failures():
    text = ("set_property -dict {PACKAGE_PIN A16 IOSTANDARD LVCMOS33} [get_ports clk]\n"
            "set_property -dict {PACKAGE_PIN A14 IOSTANDARD LVCMOS33} [get_ports rst_nn]\n"
            "set_property -dict {PACKAGE_PIN A15} [get_ports en]\n"
            "set_property -dict {PACKAGE_PIN A16 IOSTANDARD LVCMOS33} [get_ports clr]\n")
    parsed, unparsed = parse_constraints(text, "xdc")
    kinds = {f["kind"] for f in validate(expand_ports(COUNTER), parsed,
                                         unparsed=unparsed)}
    assert "unknown_port" in kinds        # rst_nn typo
    assert "unconstrained" in kinds       # rst_n, count[*], tc
    assert "missing_iostandard" in kinds  # en
    assert "duplicate_pin" in kinds       # clk and clr both on A16


def test_validate_clean_file_reports_nothing():
    text = "".join(
        f"set_property -dict {{PACKAGE_PIN P{i} IOSTANDARD LVCMOS33}} "
        f"[get_ports {{{b}}}]\n" for i, b in enumerate(expand_ports(COUNTER)))
    parsed, unparsed = parse_constraints(text, "xdc")
    assert validate(expand_ports(COUNTER), parsed, unparsed=unparsed) == []


def test_pcf_does_not_demand_an_iostandard():
    """NSTD-1 is a Vivado concept; a .pcf has no IO standard field."""
    parsed, _ = parse_constraints(
        "".join(f"set_io {b} {i}\n" for i, b in enumerate(expand_ports(COUNTER))),
        "pcf")
    assert not [f for f in validate(expand_ports(COUNTER), parsed, fmt="pcf")
                if f["kind"] == "missing_iostandard"]


# ---- boards --------------------------------------------------------------------------


_PART0 = """<?xml version="1.0"?>
<pins>
  <pin index="0" name ="clk" iostandard="LVCMOS33" loc="E3"/>
  <pin index="1" name ="led_0" iostandard="LVCMOS33" loc="H5"/>
  <pin index="2" name ="led_1" iostandard="LVCMOS33" loc="J5"/>
  <pin index="3" name ="reset" iostandard="LVCMOS33" loc="C2"/>
</pins>
"""


def _fake_board_tree(tmp_path, name="myboard", depth=("A.0", "1.0")):
    root = tmp_path / "board_files"
    d = root.joinpath(name, *depth)
    d.mkdir(parents=True)
    (d / "part0_pins.xml").write_text(_PART0)
    (d / "board.xml").write_text('<board><component name="part0" '
                                 'part_name="xc7a35ticsg324-1L"/></board>')
    return root


def test_board_discovery_handles_both_nesting_depths(tmp_path, monkeypatch):
    """Vivado nests board revisions inconsistently (<name>/<rev>/ and
    <name>/<rev>/<ver>/) — a fixed-depth glob found only a fraction."""
    root = _fake_board_tree(tmp_path, "deepboard", ("A.0", "1.0"))
    _fake_board_tree(tmp_path, "shallowboard", ("B.4",))
    monkeypatch.setenv("XILINX_VIVADO", str(tmp_path.parent))
    monkeypatch.setattr("chipchamp.physical.pins._vivado_board_dirs",
                        lambda: [str(root)])
    boards = discover_boards()
    assert set(boards) == {"deepboard", "shallowboard"}
    b = boards["deepboard"]
    assert b.part == "xc7a35ticsg324-1L" and b.source == "vivado"
    assert b.signals["clk"] == {"pin": "E3", "iostandard": "LVCMOS33"}


def test_user_board_profile_overrides_vivado(tmp_path, monkeypatch):
    root = _fake_board_tree(tmp_path, "myboard")
    monkeypatch.setattr("chipchamp.physical.pins._vivado_board_dirs",
                        lambda: [str(root)])
    dot = tmp_path / "dot"
    (dot / "boards").mkdir(parents=True)
    (dot / "boards" / "myboard.json").write_text(json.dumps(
        {"name": "myboard", "part": "custom", "format": "pcf",
         "signals": {"clk": {"pin": "99", "iostandard": ""}}}))
    ws = NS(dot=str(dot))
    b = discover_boards(ws)["myboard"]
    assert b.source == "user" and b.part == "custom"
    assert b.signals["clk"]["pin"] == "99"


def test_import_a_master_constraints_file_as_a_board():
    b = board_from_constraints(
        "icebreaker",
        "set_io CLK 35\nset_io LEDR_N 11\n", "pcf")
    assert b.source == "import" and b.signals["CLK"]["pin"] == "35"
    assert b.to_json()["name"] == "icebreaker"


# ---- generation ------------------------------------------------------------------------


def _board():
    return Board(name="b", part="p", fmt="xdc", source="test", signals={
        "clk": {"pin": "E3", "iostandard": "LVCMOS33"},
        "led_0": {"pin": "H5", "iostandard": "LVCMOS33"},
        "led_1": {"pin": "J5", "iostandard": "LVCMOS33"},
        "reset": {"pin": "C2", "iostandard": "LVCMOS33"}})


def test_generation_never_invents_a_pin():
    """THE safety property: an unmapped port is reported, never assigned some
    free pin — a wrong pin can short an output and damage hardware."""
    rows, unmapped = map_ports(expand_ports(COUNTER), _board(),
                               {"count": "led", "rst_n": "reset"})
    mapped = {r.port: r.pin for r in rows}
    assert mapped == {"clk": "E3", "rst_n": "C2",
                      "count[0]": "H5", "count[1]": "J5"}
    # the board has 2 LEDs; the remaining 6 bits + en/clr/tc stay unmapped
    assert set(unmapped) == {"en", "clr", "tc", "count[2]", "count[3]",
                             "count[4]", "count[5]", "count[6]", "count[7]"}
    text = emit_constraints(rows, "xdc", todo=unmapped)
    assert "TODO unmapped: count[7]" in text
    assert text.count("set_property") == 4          # only what was mapped


def test_generated_constraints_validate_clean_for_the_mapped_subset():
    rows, unmapped = map_ports(expand_ports(COUNTER), _board(),
                               {"count": "led", "rst_n": "reset"})
    text = emit_constraints(rows, "xdc", todo=unmapped)
    parsed, unp = parse_constraints(text, "xdc")
    assert not unp                                   # our own output parses
    found = validate([r.port for r in rows], parsed, unparsed=unp)
    assert found == []                               # no false findings
