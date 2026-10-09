"""Rendering must not invent timing, lose narrow activity or alter bus values."""
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

from chipchamp import ui
from chipchamp.cli.repl import _agent_events
from chipchamp.mcp.client import McpClient
from chipchamp.waves.raster import time_bins, text_row
from chipchamp.waves.render import ascii_timing, wavejson, fmt_time, analyzer_view, _bus_row
from chipchamp.waves.store import WaveStore
from chipchamp.waves.vcd import VcdData, VcdSignal


def snapshot(edges, end=100, width=1, start=0, **info):
    return {"window": [start, end], "timescale": "1ns", "signals": {
        "top.signal": {"width": width, "value_at_start": next((v for t, v in edges if t == start), "0"),
                       "edges": edges, **info}}}


def store(changes, width=1, end=100):
    data = VcdData(end_time=end, signals={"!": VcdSignal("!", "signal", "top", width)},
                   by_path={"top.signal": "!"}, changes={"!": changes})
    return WaveStore(data)


def test_linear_time_retains_uneven_intervals_and_late_edges():
    snap = snapshot([(0, "0"), (10, "1"), (20, "0"), (99, "1")])
    cells = time_bins(snap, 10)["top.signal"]
    assert [i for i, c in enumerate(cells) if c["transitions"]] == [1, 2, 9]
    assert [c["value"] for c in cells[3:9]] == ["0"] * 6
    assert cells[-1]["value"] == "1" and cells[-1]["compressed"]


@pytest.mark.parametrize("edges", [
    [(0, "0"), (4, "1"), (5, "0")],
    [(0, "0"), (4, "1"), (4, "0")],
    [(0, "0"), (4, "x"), (5, "z"), (6, "0")],
])
def test_subcolumn_and_same_tick_activity_stays_visible(edges):
    snap = snapshot(edges)
    cells = time_bins(snap, 10)["top.signal"]
    assert cells[0]["transitions"] >= 2
    assert text_row(cells, snap["signals"]["top.signal"])[0] == "!"
    exported = wavejson(snap)
    assert sum(c["transitions"] for c in exported["chipchamp"]["cells"]["top.signal"]) >= 2


def test_truncated_data_is_not_extended_as_a_stable_level():
    ws = store([(0, "0"), (10, "1"), (20, "0"), (90, "1")])
    snap = ws.snapshot(["top.signal"], 0, 100, max_edges=2)
    info = snap["signals"]["top.signal"]
    assert info["omitted_edges"] == 2 and info["truncated_at"] == 20
    cells = time_bins(snap, 10)["top.signal"]
    assert not cells[1]["unknown"] and all(c["unknown"] for c in cells[2:])
    rendered = ascii_timing(snap)
    assert "Transition limit reached" in rendered and "?" * 5 in rendered


def test_unrecorded_prefix_and_tail_are_unknown():
    ws = store([(20, "1")], end=50)
    snap = ws.snapshot(["top.signal"], 0, 100)
    cells = time_bins(snap, 10)["top.signal"]
    assert all(c["unknown"] for c in cells[:2])
    assert not cells[2]["unknown"]
    assert all(c["unknown"] for c in cells[5:])


def test_z_x_real_and_large_bus_values_are_not_mislabelled():
    for value, expected in (("x", "X"), ("z", "Z")):
        snap = snapshot([(0, value)])
        assert text_row(time_bins(snap, 8)["top.signal"], snap["signals"]["top.signal"]) == expected * 8
    snap = snapshot([(0, "1010110011011110")], width=16)
    assert text_row(time_bins(snap, 2)["top.signal"], snap["signals"]["top.signal"]) == "…═"
    assert "acde" in wavejson(snap)["signal"][0]["data"]
    assert Text.from_markup(_bus_row(["1010110011011110"])).plain == "…═"
    real = snapshot([(0, "10")], width=64, var_type="real")
    assert wavejson(real)["signal"][0]["data"] == ["10"]
    mixed = snapshot([(0, "10xz")], width=4)
    assert wavejson(mixed)["signal"][0]["data"] == ["b10xz"]


def test_edge_positions_use_integer_time_beyond_double_precision():
    origin = 2**60
    snap = snapshot([(origin, "0"), (origin + 37, "1")], start=origin, end=origin + 100)
    cells = time_bins(snap, 100)["top.signal"]
    assert [i for i, c in enumerate(cells) if c["transitions"]] == [37]
    assert fmt_time(1, "1fs") == "1fs" and fmt_time(100, "10fs") == "1ps"


def test_zero_duration_and_unknown_signal_are_explicit():
    ws = store([(0, "0"), (10, "1")])
    snap = ws.snapshot(["top.signal", "absent"], 10, 10)
    assert len(time_bins(snap, 100)["top.signal"]) == 1
    assert wavejson(snap)["chipchamp"]["missing_signals"] == ["absent"]
    assert "absent" in ascii_timing(snap)
    with pytest.raises(ValueError):
        ws.snapshot(["top.signal"], 20, 10)


def test_ambiguous_leaf_names_never_select_an_arbitrary_signal():
    ws = store([(0, "0")])
    ws.data.by_path.update({"other.signal": "#"})
    assert ws.data.id_for("signal") is None
    assert ws.data.id_for("top.signal") == "!"
    assert ws.snapshot(["signal"], 0, 10)["missing_signals"] == ["signal"]


def test_sampled_analyzer_marks_activity_between_clock_edges():
    ws = store([(0, "0"), (6, "1"), (7, "0")], end=30)
    ws.data.signals["c"] = VcdSignal("c", "clk", "top", 1)
    ws.data.by_path["top.clk"] = "c"
    ws.data.changes["c"] = [(0, "0"), (5, "1"), (10, "0"), (15, "1"), (20, "0"), (25, "1")]
    text = "\n".join(analyzer_view(ws, paths=["top.clk", "top.signal"]))
    assert "[yellow]!!" in text and "activity between samples" in text


def client(tmp_path, blocks):
    c = McpClient.__new__(McpClient)
    c.name, c.image_dir = "wavelets", tmp_path
    c._request = lambda *args: {"content": blocks}
    return c


def test_multiple_images_and_calls_never_overwrite_each_other(tmp_path):
    blocks = [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}
              for data in (b"first", b"second")]
    c = client(tmp_path, blocks)
    a, b = c.call("render_waveform_png", {}), c.call("render_waveform_png", {})
    paths = [im["path"] for result in (a, b) for im in result["images"]]
    assert len(set(paths)) == 4
    assert [Path(p).read_bytes() for p in paths] == [b"first", b"second"] * 2


def test_mcp_text_is_saved_and_shown_verbatim_without_markdown(tmp_path, monkeypatch):
    text = "waveform  [0ns .. 10ns]\nclk[0]   ▁▁╱▔▔\ndata     000═╳abc  "
    result = client(tmp_path, [{"type": "text", "text": text}]).call("render_waveform", {})
    assert Path(result["waveform"]["path"]).read_text() == text
    capture = io.StringIO()
    console = Console(file=capture, width=100, force_terminal=False)
    monkeypatch.setattr(ui, "_console", console)
    monkeypatch.setattr(ui, "markdown", lambda *a: pytest.fail("waveform must bypass Markdown"))
    c = SimpleNamespace(runner=SimpleNamespace(), edits={}, ws=SimpleNamespace(config={}, root=str(tmp_path)))
    _agent_events(c)("tool_result", {"name": "mcp.wavelets.render_waveform", "result": result})
    assert text in capture.getvalue()


def test_external_text_blocks_and_png_caption_are_preserved(tmp_path):
    blocks = [{"type": "text", "text": "PNG waveform snapshot"},
              {"type": "text", "text": "waveform  [0ns .. 10ns]\nclk ▁╱▔"}]
    result = client(tmp_path, blocks).call("render_waveform_png", {})
    assert result["text"] == "\n".join(b["text"] for b in blocks)
    assert result["waveform"]["format"] == "text"


def test_narrow_terminal_marks_cropping_instead_of_wrapping_waveform(monkeypatch):
    capture = io.StringIO()
    monkeypatch.setattr(ui, "_console", Console(file=capture, width=20))
    ui.waveform("data  " + "▁" * 30)
    lines = capture.getvalue().splitlines()
    assert lines[0].endswith("→") and len(lines[0]) == 20
    assert "▁" not in lines[1]
    assert "cropped" in capture.getvalue()


def test_kitty_image_bytes_and_terminal_width_are_preserved(tmp_path, monkeypatch, capsys):
    # A payload larger than a protocol chunk tests reconstruction, not just the
    # first escape code. Sending JPEG with f=100 used to produce a broken image.
    raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 30
    path = tmp_path / "wave.png"
    path.write_bytes(raw)
    monkeypatch.setattr(ui, "image_protocol", lambda: "kitty")
    assert ui.inline_image(str(path), max_cols=78)
    output = capsys.readouterr().out
    assert "a=T,f=100,c=78,m=1" in output
    chunks = [p.split(";", 1)[1].split("\x1b\\", 1)[0] for p in output.split("\x1b_G")[1:]]
    assert base64.b64decode("".join(chunks)) == raw
    jpg = tmp_path / "image.jpg"
    jpg.write_bytes(b"\xff\xd8\xff")
    assert not ui.inline_image(str(jpg))
