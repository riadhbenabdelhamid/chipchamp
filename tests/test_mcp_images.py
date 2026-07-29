"""MCP image blocks: reach the terminal, never the transcript.

An MCP reply can carry several content blocks — wavelets' render_waveform_png
sends the picture AND a caption. The client used to return at the first text
block and drop the picture entirely. Keeping the picture inline would have been
worse: a PNG is kilobytes of base64, and the transcript is a budgeted resource
(agent/working_set.py). So an image is spilled to a file and only its path
travels.
"""
from __future__ import annotations

import base64
import json

import pytest

from chipchamp import ui
from chipchamp.mcp.client import McpClient

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 600).decode()


def _client(tmp_path, blocks):
    """An McpClient with the transport stubbed — only `call`'s unwrapping is
    under test, and spawning a real server would test subprocess plumbing."""
    c = McpClient.__new__(McpClient)
    c.name = "wavelets"
    c.image_dir = tmp_path / "mcp-images"
    c._request = lambda method, params=None: {"content": blocks}
    return c


def test_an_image_block_is_spilled_and_only_its_path_travels(tmp_path):
    c = _client(tmp_path, [
        {"type": "image", "data": PNG, "mimeType": "image/png"},
        {"type": "text", "text": "PNG waveform snapshot (608 bytes)"}])
    out = c.call("render_waveform_png", {})
    assert out["text"].startswith("PNG waveform snapshot")
    assert len(out["images"]) == 1
    im = out["images"][0]
    assert im["mime"] == "image/png" and im["bytes"] == 608
    # the picture is on disk…
    assert open(im["path"], "rb").read().startswith(b"\x89PNG")
    # …and the base64 is NOT in what the model will read
    assert PNG[:64] not in json.dumps(out)
    assert len(json.dumps(out)) < 400


def test_a_text_only_reply_is_unchanged(tmp_path):
    """The ordinary path must not grow an empty `images` key."""
    c = _client(tmp_path, [{"type": "text", "text": '{"hits": 3}'}])
    assert c.call("find_when", {}) == {"hits": 3}


def test_an_image_only_reply_still_yields_the_path(tmp_path):
    c = _client(tmp_path, [{"type": "image", "data": PNG, "mimeType": "image/png"}])
    out = c.call("render", {})
    assert out["images"] and "text" not in out


def test_a_corrupt_image_block_is_dropped_not_fatal(tmp_path):
    c = _client(tmp_path, [{"type": "image", "data": "!!not base64!!",
                            "mimeType": "image/png"},
                           {"type": "text", "text": "caption"}])
    out = c.call("render", {})
    assert out["text"] == "caption"          # the call still succeeds
    assert "images" not in out               # nothing claimed that isn't there


def test_svg_keeps_its_extension(tmp_path):
    c = _client(tmp_path, [{"type": "image", "data": base64.b64encode(b"<svg/>").decode(),
                            "mimeType": "image/svg+xml"}])
    assert c.call("render_waveform_svg", {})["images"][0]["path"].endswith(".svg")


# ---- the terminal side ------------------------------------------------------

def test_no_inline_protocol_means_no_escape_sequences(monkeypatch, capsys):
    """Spraying a graphics escape at a terminal that cannot read it is worse
    than printing a path."""
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    for var in ("KITTY_WINDOW_ID", "TERM_PROGRAM", "WEZTERM_PANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert ui.image_protocol() == ""
    assert ui.inline_image("/whatever.png") is False


def test_protocols_are_detected(monkeypatch):
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    for var in ("KITTY_WINDOW_ID", "TERM_PROGRAM", "WEZTERM_PANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-kitty")
    assert ui.image_protocol() == "kitty"
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert ui.image_protocol() == "iterm2"


def test_an_svg_is_never_drawn_inline(monkeypatch, tmp_path):
    """There is no inline protocol for SVG — it must be linked, not mangled."""
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    f = tmp_path / "w.svg"
    f.write_text("<svg/>")
    assert ui.inline_image(str(f)) is False


def test_off_links_instead_of_drawing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "inline_image", lambda *a, **k: pytest.fail(
        "mode=off must not attempt to draw"))
    f = tmp_path / "w.png"
    f.write_bytes(b"\x89PNG")
    ui.show_images([{"path": str(f), "bytes": 4}], mode="off")
    assert "w.png" in capsys.readouterr().out


def test_image_mode_says_why_it_could_not_draw(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "image_protocol", lambda: "")
    ui.show_images([{"path": str(tmp_path / "w.png"), "bytes": 9}], mode="image")
    assert "no inline-image protocol" in capsys.readouterr().out


def test_the_setting_is_read_and_validated():
    from chipchamp.cli import _image_mode
    def ws(v):
        return type("C", (), {"ws": type("W", (), {"config": {"ui": {"mcp_images": v}}})()})()
    assert _image_mode(ws("off")) == "off"
    assert _image_mode(ws("IMAGE")) == "image"
    assert _image_mode(ws("nonsense")) == "auto"     # never a crash, never a surprise
    assert _image_mode(type("C", (), {"ws": type("W", (), {"config": {}})()})()) == "auto"
