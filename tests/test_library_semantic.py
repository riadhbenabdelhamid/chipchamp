"""Semantic + interface IP-component matching: name-independent functional
match, interface signature/compatibility, hybrid ranking, graceful fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chipchamp import embeddings as emb
from chipchamp import library_semantic as ls
from chipchamp.tools import all_tools


def _mk_ws(tmp_path, config_text=""):
    from chipchamp.config import Workspace
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "config.toml").write_text(config_text or "[project]\nname='t'\n")
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    return Workspace(str(tmp_path))


def _mk_library(root: Path) -> Path:
    """A tiny 3-component library exercising the interface metadata."""
    root.mkdir(parents=True, exist_ok=True)
    for cat, name in (("storage/fifo", "sync_fifo"),
                      ("arbitration/arbiter", "rr_arbiter"),
                      ("bus/axil", "axil_bridge")):
        d = root / cat.split("/")[0] / name
        (d / "rtl").mkdir(parents=True)
        (d / "rtl" / f"{name}.sv").write_text(f"module {name}; endmodule\n")
    manifest = {"library": "mini", "modules": [
        {"name": "sync_fifo", "category": "storage/fifo",
         "summary": "Synchronous FIFO buffer with ready/valid ports.",
         "description": "Elastic queue that buffers data and applies "
                        "backpressure between a producer and consumer.",
         "tags": ["fifo", "queue", "buffer", "elastic"],
         "clocks": [{"name": "clk_i"}], "resets": [{"name": "rst_ni"}],
         "params": [{"name": "Width"}, {"name": "Depth"}],
         "ports": [{"name": "wr_valid_i", "role": "valid"},
                   {"name": "wr_ready_o", "role": "ready"},
                   {"name": "wr_data_i", "role": "data"},
                   {"name": "rd_valid_o", "role": "valid"},
                   {"name": "rd_ready_i", "role": "ready"}]},
        {"name": "rr_arbiter", "category": "arbitration/arbiter",
         "summary": "Round-robin arbiter granting one of N requesters.",
         "description": "Fair arbitration among competing requesters.",
         "tags": ["arbiter", "round-robin"],
         "clocks": [{"name": "clk_i"}], "resets": [{"name": "rst_ni"}],
         "ports": [{"name": "req_i", "role": "request"},
                   {"name": "gnt_o", "role": "grant"}]},
        {"name": "axil_bridge", "category": "bus/axil",
         "summary": "AXI4-Lite protocol bridge.",
         "description": "Bridges AXI4-Lite between two ports.",
         "tags": ["axi4-lite", "bus"],
         "interfaces": [{"type": "axi4-lite", "dir": "completer"}],
         "clocks": [{"name": "s_clk"}, {"name": "m_clk"}],
         "resets": [{"name": "s_rst"}, {"name": "m_rst"}],
         "ports": [{"name": "awvalid", "role": "valid"},
                   {"name": "awready", "role": "ready"}]},
    ]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


# ---- axis fake embedder (keyword -> vector) ------------------------------------


AXES = (("fifo", "queue", "buffer", "elastic", "backpressure"),  # storage
        ("arbiter", "arbitration", "round-robin", "grant"),      # arbitration
        ("axi", "axi4-lite", "bridge", "bus", "protocol"))       # bus


def _fake_embed(ws, texts, timeout, provider="", model=""):
    out = []
    for t in texts:
        low = t.lower()
        vec = [0.05, 0.05, 0.05]
        for i, words in enumerate(AXES):
            if any(w in low for w in words):
                vec[i] = 1.0
        out.append(vec)
    return out


@pytest.fixture()
def lib_ws(tmp_path, monkeypatch):
    root = _mk_library(tmp_path / "mini")
    ws = _mk_ws(tmp_path / "work",
                config_text=f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    monkeypatch.setattr(emb, "embed", _fake_embed)
    monkeypatch.setattr(emb, "resolve_endpoint", lambda w, p="", m="": ("fake://", "e"))
    emb._RESOLVED.clear()
    return ws


# ---- interface signature -------------------------------------------------------


def test_interface_signature(lib_ws):
    from chipchamp.library import discover
    mods = discover(lib_ws)
    sig = ls.interface_signature(mods["sync_fifo"])
    assert "valid/ready handshake" in sig
    assert "single-clock" in sig
    assert "Width" in sig and "Depth" in sig
    axil = ls.interface_signature(mods["axil_bridge"])
    assert "axil" in axil  # axi4-lite normalized
    assert "async/dual-clock" in axil


# ---- name-independent functional match -----------------------------------------


def test_match_finds_fifo_from_behavior_not_name(lib_ws):
    # describe the block WITHOUT saying 'fifo' — semantic must still find it
    hits = ls.match(lib_ws,
                    behavior="a buffer that holds streaming data and applies "
                             "backpressure between producer and consumer",
                    interface="valid/ready sink and source, single clock")
    assert hits[0][0].name == "sync_fifo"
    info = hits[0][2]
    assert info["semantic"] > 0.5
    assert "valid/ready handshake" in info["why"]


def test_match_protocol_interface_boosts(lib_ws):
    hits = ls.match(lib_ws, behavior="connect two bus ports",
                    interface="AXI4-Lite")
    top = hits[0]
    assert top[0].name == "axil_bridge"
    assert "protocol axil" in top[2]["why"]


def test_interface_compat_only_scores_stated_dims(lib_ws):
    from chipchamp.library import discover
    fifo = discover(lib_ws)["sync_fifo"]
    # query states a handshake constraint the fifo satisfies -> full
    score, why = ls.interface_compat("valid/ready backpressure", fifo)
    assert score == 1.0 and "valid/ready handshake" in why
    # query states an async/CDC constraint the single-clock fifo does NOT meet
    score2, _ = ls.interface_compat("async cdc dual-clock crossing", fifo)
    assert score2 == 0.0
    # query with no interface hint at all -> neutral 0.5
    score3, _ = ls.interface_compat("something", fifo)
    assert score3 == 0.5


# ---- fallback (no embeddings) --------------------------------------------------


def test_match_falls_back_to_lexical_plus_interface(tmp_path, monkeypatch):
    root = _mk_library(tmp_path / "mini")
    ws = _mk_ws(tmp_path / "work2",
                config_text=f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    monkeypatch.setattr(emb, "embed",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no endpoint")))
    emb._RESOLVED.clear()
    hits = ls.match(ws, behavior="round-robin arbiter granting requesters")
    assert hits and hits[0][0].name == "rr_arbiter"  # lexical still finds it
    assert hits[0][2]["semantic"] == 0.0             # no embeddings used


# ---- tool ----------------------------------------------------------------------


def test_lib_match_tool(lib_ws):
    from chipchamp.tools.context import ToolContext
    ctx = ToolContext(lib_ws)
    res = all_tools()["lib.match"].handler(
        ctx, behavior="elastic buffer with backpressure",
        interface="valid/ready")
    assert res["matches"][0]["name"] == "sync_fifo"
    m0 = res["matches"][0]
    assert "interface" in m0 and "why" in m0 and "score" in m0


# ---- live corpus ---------------------------------------------------------------


import urllib.request  # noqa: E402


def _emb_up() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:1234/v1/models",
                                    timeout=2) as r:
            return any("embed" in m.get("id", "")
                       for m in json.load(r).get("data", []))
    except Exception:
        return False


LIVE = Path(os.path.expanduser("~/rtllib-pillars"))


@pytest.mark.skipif(not (_emb_up() and (LIVE / "manifest.json").is_file()),
                    reason="needs LM Studio embedding model + ~/rtllib-pillars")
def test_live_match_names_differ(tmp_path):
    ws = _mk_ws(tmp_path / "work",
                config_text=f"[project]\nname='t'\n[library]\ndirs=['{LIVE}']\n")
    emb._RESOLVED.clear()
    # describe a CDC async FIFO purely by behavior + interface, no 'fifo_async'
    hits = ls.match(ws, behavior="move data across two asynchronous clock "
                    "domains without losing or duplicating beats",
                    interface="ready/valid on both sides, two clocks", limit=8)
    names = [m.name for m, _, _ in hits]
    assert any("fifo" in n or "cdc" in n or "async" in n for n in names), names
