"""Waveform service and coverage service (parsing + queries + baseline gate)."""
from __future__ import annotations

import textwrap

from chipchamp.coverage import Bin, CoverageModel, CoverageService
from chipchamp.waves.store import WaveStore
from chipchamp.waves.vcd import parse_vcd

VCD = textwrap.dedent("""\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 # vld $end
$var wire 4 $ cnt $end
$upscope $end
$enddefinitions $end
#0
0!
0#
b0 $
#10
1!
1#
b101 $
#20
0!
1#
b1111 $
#30
1!
0#
b0 $
""")


def _store(tmp_path):
    p = tmp_path / "d.vcd"
    p.write_text(VCD)
    return WaveStore.open(str(p))


def test_vcd_parse(tmp_path):
    d = parse_vcd(str(tmp_path / "d.vcd")) if False else parse_vcd_str(tmp_path)
    assert d.by_path["tb.vld"]
    assert d.end_time == 30


def parse_vcd_str(tmp_path):
    p = tmp_path / "d.vcd"
    p.write_text(VCD)
    return parse_vcd(str(p))


def test_value_at_time(tmp_path):
    ws = _store(tmp_path)
    assert ws.value("tb.vld", 5) == "0"
    assert ws.value("tb.vld", 15) == "1"
    assert ws.value("tb.cnt", 25) == "1111"


def test_when_expression(tmp_path):
    ws = _store(tmp_path)
    r = ws.when("vld && (cnt == 4'hF)")
    assert r["time"] == 20


def test_changes_window(tmp_path):
    ws = _store(tmp_path)
    ch = ws.changes("tb.cnt", 0, 30)
    assert len(ch["transitions"]) == 4


def test_compare_no_divergence(tmp_path):
    ws = _store(tmp_path)
    assert ws.compare(ws)["first_divergence"] is None


def test_coverage_rates_and_holes():
    m = CoverageModel()
    m.add_bin(Bin("g::p::a", "g", "p", "a", count=3, section="S1"), test="t1")
    m.add_bin(Bin("g::p::b", "g", "p", "b", count=0, section="S1"))
    svc = CoverageService(m)
    assert svc.summary()["total"] == 50.0
    holes = svc.holes(kind="functional")
    assert holes["total_holes"] == 1
    assert holes["holes"][0]["bin"] == "g::p::b"


def test_coverage_baseline_gate():
    base = CoverageModel()
    base.add_bin(Bin("g::p::a", "g", "p", "a", count=1, section="S1"))
    base.add_bin(Bin("g::p::b", "g", "p", "b", count=1, section="S1"))
    cur = CoverageModel()
    cur.add_bin(Bin("g::p::a", "g", "p", "a", count=1, section="S1"))
    cur.add_bin(Bin("g::p::b", "g", "p", "b", count=0, section="S1"))  # regressed
    v = CoverageService(cur).baseline_check(base)
    assert not v.ok and v.total_delta < 0


# ---- tier-3 liveness: inline logic-analyzer view -----------------------------

ANALYZER_VCD = textwrap.dedent("""\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 " rst_ni $end
$var wire 1 # m_valid $end
$var wire 1 % m_ready $end
$var wire 8 $ data[7:0] $end
$upscope $end
$enddefinitions $end
#0
0!
0"
0#
0%
b0 $
#5
1!
#10
0!
1"
#15
1!
1#
1%
b10100110 $
#20
0!
#25
1!
#30
0!
0#
#35
1!
b1111111 $
#40
0!
#45
1!
bxxxxxxxx $
#50
0!
""")


def _analyzer_store(tmp_path):
    p = tmp_path / "an.vcd"
    p.write_text(ANALYZER_VCD)
    return WaveStore.open(str(p))


def test_pick_signals_ranks_clock_then_handshakes(tmp_path):
    from chipchamp.waves.render import pick_signals
    ws = _analyzer_store(tmp_path)
    picked = pick_signals(ws.data)
    names = [p.rsplit(".", 1)[-1] for p in picked]
    assert names[0] == "clk" and names[1] == "rst_ni"
    assert names[2:4] == ["m_valid", "m_ready"]   # handshakes before payload
    assert any(n.startswith("data") for n in names)


def test_analyzer_view_renders_bits_buses_and_ruler(tmp_path):
    from chipchamp.waves.render import analyzer_view
    ws = _analyzer_store(tmp_path)
    lines = analyzer_view(ws, width=100)
    text = "\n".join(lines)
    assert "cycles of" in lines[0] and "clk" in lines[0]  # clock-sampled
    assert "▔" in text and "▁" in text                    # 1-bit contrast
    # bus: a 2-cycle-stable value renders its full hex label; single-cycle
    # segments truncate (like a real analyzer); X in red; ╳ transition marks
    assert "a6" in text and "╳" in text
    assert "[red]x" in text
    assert "╵" in lines[-1]                               # ruler


def test_analyzer_view_without_clock_falls_back_to_time(tmp_path):
    from chipchamp.waves.render import analyzer_view
    vcd = ANALYZER_VCD.replace("clk", "sig")
    p = tmp_path / "noclk.vcd"
    p.write_text(vcd)
    ws = WaveStore.open(str(p))
    lines = analyzer_view(ws, width=80)
    assert lines and "time" in lines[0]


def test_analyzer_view_empty_store_is_empty(tmp_path):
    from chipchamp.waves.render import analyzer_view
    p = tmp_path / "empty.vcd"
    p.write_text("$timescale 1ns $end\n$enddefinitions $end\n")
    ws = WaveStore.open(str(p))
    assert analyzer_view(ws) == []


def _vcd(tmp_path, body: str):
    """Minimal two-signal VCD; `body` supplies the value changes."""
    p = tmp_path / "t.vcd"
    p.write_text("$timescale 1ns $end\n"
                 "$scope module tb $end\n"
                 "$var wire 1 ! toggling $end\n"
                 "$var parameter 32 @ DEPTH $end\n"
                 "$var integer 32 # errors $end\n"
                 "$var wire 1 $ stuck_x $end\n"
                 "$upscope $end\n$enddefinitions $end\n" + body)
    return p


def test_pick_signals_drops_signals_that_never_toggle(tmp_path):
    """DEPTH is a parameter and errors a plain integer that merely never moves
    — filtering on var_type would catch one and miss the other, so the value
    HISTORY is what decides. On the example SoC three of eight rows went to
    constants, pushing out the handshakes the bug was visible in."""
    from chipchamp.waves import WaveStore
    from chipchamp.waves.render import pick_signals
    v = _vcd(tmp_path, "#0\n0!\nb1000 @\nb0 #\n0$\n#10\n1!\n#20\n0!\n")
    picked = [p.rsplit(".", 1)[-1] for p in pick_signals(WaveStore.open(str(v)).data)]
    assert "toggling" in picked
    assert "DEPTH" not in picked and "errors" not in picked


def test_a_signal_stuck_at_x_is_never_hidden(tmp_path):
    """It never toggles either — but it is a defect, and a picker that hid it
    would hide exactly what someone opened the waveform to find."""
    from chipchamp.waves import WaveStore
    from chipchamp.waves.render import pick_signals
    v = _vcd(tmp_path, "#0\n0!\nb1000 @\nb0 #\nx$\n#10\n1!\n")
    picked = [p.rsplit(".", 1)[-1] for p in pick_signals(WaveStore.open(str(v)).data)]
    assert "stuck_x" in picked
    assert "DEPTH" not in picked


def test_an_entirely_static_trace_still_renders(tmp_path):
    """Better a view of flat lines than an empty panel that reads as a
    rendering failure."""
    from chipchamp.waves import WaveStore
    from chipchamp.waves.render import pick_signals
    v = _vcd(tmp_path, "#0\n0!\nb1000 @\nb0 #\n0$\n")
    assert pick_signals(WaveStore.open(str(v)).data)     # not empty
