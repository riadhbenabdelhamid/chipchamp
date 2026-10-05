"""config.local.toml: what is true on THIS machine (tool paths, MCP servers,
model endpoints) merges over the tracked config.toml and never ships."""
from __future__ import annotations

from chipchamp.config import Workspace, _deep_merge


def test_tables_merge_and_scalars_replace():
    base = {"project": {"name": "soc", "work_dir": "work"},
            "mcp": {"servers": {"a": {"command": "a"}}}, "tools": {"lint": "verilator"}}
    over = {"project": {"work_dir": "scratch"},
            "mcp": {"servers": {"b": {"command": "b"}}}, "tools": "replaced"}
    out = _deep_merge(base, over)
    assert out["project"] == {"name": "soc", "work_dir": "scratch"}
    assert set(out["mcp"]["servers"]) == {"a", "b"}
    assert out["tools"] == "replaced"
    assert base["mcp"]["servers"] == {"a": {"command": "a"}}      # base untouched


def test_workspace_applies_the_local_overlay(tmp_path):
    dot = tmp_path / ".chipchamp"
    dot.mkdir()
    (dot / "config.toml").write_text('[project]\nname = "soc"\n[tools]\nlint = "verilator"\n')
    (dot / "config.local.toml").write_text(
        '[mcp.servers.local]\ncommand = "my-server --mcp"\n[tools]\nsynth = "yosys"\n')
    ws = Workspace(str(tmp_path))
    assert ws.config["mcp"]["servers"]["local"]["command"] == "my-server --mcp"
    assert ws.config["tools"] == {"lint": "verilator", "synth": "yosys"}
    assert ws.config["project"]["name"] == "soc"


def test_the_tracked_example_names_no_machine(tmp_path):
    from conftest import REAL_EXAMPLE
    text = (REAL_EXAMPLE / ".chipchamp" / "config.toml").read_text()
    assert "/home/" not in text                     # no absolute path of anyone's
    live = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("[mcp.servers.") for ln in live)   # placeholder only
