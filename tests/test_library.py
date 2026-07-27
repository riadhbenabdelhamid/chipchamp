"""IP component library: manifest discovery + registry, search, cards,
instantiation templates, policy-gated fetch, prompt index.
Live test against the user's real corpus at ~/rtllib-pillars when present."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chipchamp import library as lib
from chipchamp.tools import all_tools


def _mk_ws(tmp_path, config_text=""):
    from chipchamp.config import Workspace
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "config.toml").write_text(config_text or "[project]\nname='t'\n")
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    return Workspace(str(tmp_path))


def _ctx_for(ws):
    from chipchamp.tools.context import ToolContext
    return ToolContext(ws)


def _mk_library(root: Path, with_core: bool = True) -> Path:
    """A minimal two-component library in the riscvier layout."""
    root.mkdir(parents=True, exist_ok=True)
    common = root / "common" / "rtl"
    common.mkdir(parents=True)
    (common / "registers.svh").write_text("`define FF(q,d,clk) always_ff @(posedge clk) q <= d;\n")
    (common / "assertions.svh").write_text("// assertion helpers\n")
    (common / "minilib_pkg.sv").write_text("package minilib_pkg; endpackage\n")

    fifo = root / "storage" / "alpha_fifo"
    (fifo / "rtl").mkdir(parents=True)
    (fifo / "rtl" / "alpha_fifo.sv").write_text(
        '`include "registers.svh"\nmodule alpha_fifo #(parameter int Width=8)'
        "(input logic clk_i, input logic [Width-1:0] d_i, output logic [Width-1:0] q_o);"
        "endmodule\n")
    if with_core:
        (fifo / "alpha_fifo.core").write_text(
            "CAPI=2:\n"
            'name: "minilib:storage:alpha_fifo:1.0.0"\n'
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - ../../common/rtl/registers.svh: {is_include_file: true}\n"
            "      - ../../common/rtl/minilib_pkg.sv\n"
            "      - rtl/alpha_fifo.sv\n"
            "    file_type: systemVerilogSource\n")

    arb = root / "arbitration" / "beta_arb"
    (arb / "rtl").mkdir(parents=True)
    (arb / "rtl" / "beta_arb.sv").write_text(
        "module beta_arb(input logic clk_i); endmodule\n")

    manifest = {
        "library": "minilib",
        "count": 2,
        "modules": [
            {"name": "alpha_fifo", "version": "1.0.0",
             "category": "storage/fifo",
             "summary": "A tiny FIFO used for tests.",
             "description": "Test FIFO with ready/valid.",
             "tags": ["fifo", "storage"], "status": "verified",
             "top": "alpha_fifo", "language": "systemverilog",
             "params": [{"name": "Width", "type": "int", "default": 8,
                         "desc": "data width"}],
             "clocks": [{"name": "clk_i", "edge": "rising"}],
             "resets": [],
             "ports": [{"name": "d_i", "dir": "in", "width": "Width"},
                       {"name": "q_o", "dir": "out", "width": "Width"}]},
            {"name": "beta_arb", "version": "1.0.0",
             "category": "arbitration/arbiter",
             "summary": "A tiny arbiter used for tests.",
             "tags": ["arbiter"], "status": "experimental",
             "top": "beta_arb",
             "params": [], "clocks": [{"name": "clk_i"}], "ports": []},
            {"name": "ghost", "version": "1.0.0", "category": "storage/fifo",
             "summary": "manifest entry without a directory."},
        ]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


# ---- discovery + registry ------------------------------------------------------


def test_discovery_layers_and_warnings(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    assert lib.discover(ws) == {}

    lib.add_registry_dir(ws, str(root))
    rep = lib.discover_report(ws)
    assert set(rep.modules) == {"alpha_fifo", "beta_arb"}
    assert rep.libraries[0]["name"] == "minilib"
    assert rep.libraries[0]["count"] == 2
    # the ghost entry warns but never crashes discovery
    assert any("ghost" in w for w in rep.warnings)
    assert rep.modules["alpha_fifo"].source == "registry"

    # config layer wins over registry for the same content
    ws2 = _mk_ws(tmp_path / "work2",
                 config_text=f"[project]\nname='t'\n[library]\ndirs=['{root}']\n")
    assert lib.discover_report(ws2).modules["alpha_fifo"].source == "config"

    # remove --all empties the registry layer
    lib.remove_registry_dir(ws, all=True)
    assert lib.discover(ws) == {}


def test_add_requires_manifest(tmp_path):
    ws = _mk_ws(tmp_path / "work")
    plain = tmp_path / "no_manifest"
    plain.mkdir()
    lib.add_registry_dir(ws, str(plain))
    # registered but not a valid library root -> not scanned
    assert lib.discover(ws) == {}


# ---- search ---------------------------------------------------------------------


def test_search_ranking_and_filters(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    lib.add_registry_dir(ws, str(root))

    hits = lib.search(ws, query="fifo")
    assert hits and hits[0].name == "alpha_fifo"
    assert lib.search(ws, query="arbiter")[0].name == "beta_arb"
    assert lib.search(ws, query="fifo", category="arbitration") == []
    assert lib.search(ws, query="zzz-nothing") == []
    # empty query with a category filter lists that subtree
    assert [m.name for m in lib.search(ws, category="storage")] == ["alpha_fifo"]


# ---- files + instantiation ------------------------------------------------------


def test_files_for_core_and_fallback(tmp_path):
    root = _mk_library(tmp_path / "minilib", with_core=True)
    ws = _mk_ws(tmp_path / "work")
    lib.add_registry_dir(ws, str(root))
    mods = lib.discover(ws)

    files = lib.files_for(mods["alpha_fifo"])  # from the .core fileset
    rels = [f["rel"] for f in files]
    assert rels == ["registers.svh", "minilib_pkg.sv", "alpha_fifo.sv"]
    assert files[0]["include"] is True and files[2]["include"] is False

    # no .core -> convention fallback (common includes + rtl/*.sv)
    files2 = lib.files_for(mods["beta_arb"])
    rels2 = [f["rel"] for f in files2]
    assert "beta_arb.sv" in rels2 and "registers.svh" in rels2


def test_instantiation_template(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    lib.add_registry_dir(ws, str(root))
    snippet = lib.instantiation(lib.discover(ws)["alpha_fifo"])
    assert "alpha_fifo #(" in snippet
    assert ".Width(8)" in snippet
    assert ".clk_i()" in snippet and ".q_o()" in snippet
    assert snippet.rstrip().endswith(");")


# ---- tools ----------------------------------------------------------------------


def test_lib_tools_search_info_fetch(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    lib.add_registry_dir(ws, str(root))
    ctx = _ctx_for(ws)
    tools = all_tools()

    res = tools["lib.search"].handler(ctx, query="fifo")
    assert res["matches"][0]["name"] == "alpha_fifo"

    info = tools["lib.info"].handler(ctx, name="alpha_fifo")
    assert info["params"][0]["name"] == "Width"
    assert "instantiation" in info and info["files"]

    bad = tools["lib.info"].handler(ctx, name="alpha_fif")
    assert "error" in bad and "alpha_fifo" in bad["error"]

    fetched = tools["lib.fetch"].handler(ctx, name="alpha_fifo")
    # default dest lives INSIDE the work dir so the inferred target compiles it
    assert sorted(fetched["written"]) == [
        "work/rtl/lib/alpha_fifo.sv", "work/rtl/lib/minilib_pkg.sv",
        "work/rtl/lib/registers.svh"]
    for f in fetched["written"]:
        assert (Path(ws.root) / f).is_file()
    # fetched files are recorded edits (visible to gating)
    assert set(fetched["written"]) <= set(ctx.edits)

    again = tools["lib.fetch"].handler(ctx, name="alpha_fifo")
    assert again["written"] == [] and len(again["unchanged"]) == 3


def test_lib_fetch_respects_policy_deny(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    lib.add_registry_dir(ws, str(root))
    ctx = _ctx_for(ws)
    denied = all_tools()["lib.fetch"].handler(
        ctx, name="alpha_fifo", dest="pdk/stdcells")
    assert denied["written"] == []
    assert denied["denied"] and all("pdk" in d["path"] for d in denied["denied"])


# ---- prompt index ---------------------------------------------------------------


def test_library_index_modes(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    assert lib.library_index(ws) == ""  # no libraries -> no section
    lib.add_registry_dir(ws, str(root))
    idx = lib.library_index(ws, "compact")
    assert "minilib: 2 components" in idx
    assert "storage(1)" in idx and "arbitration(1)" in idx
    full = lib.library_index(ws, "full")
    assert "alpha_fifo" in full and "beta_arb" in full
    assert lib.library_index(ws, "off") == ""


def test_prompt_carries_library_section(tmp_path):
    root = _mk_library(tmp_path / "minilib")
    ws = _mk_ws(tmp_path / "work")
    lib.add_registry_dir(ws, str(root))
    from chipchamp.agent.prompts import build_system_prompt
    prompt = build_system_prompt(ws, "", "policy")
    assert "IP library" in prompt and "lib.search" in prompt


def test_prompt_teaches_recursive_decompose_then_reuse(tmp_path):
    """The reuse posture must be a recursive top-down tree walk that matches
    the library at EVERY node (not just directly under the top) and prunes a
    branch when a component covers the whole sub-tree (the matrix-mul autopsy
    + the 'leaf is any childless node at any level' correction)."""
    ws = _mk_ws(tmp_path / "work")
    from chipchamp.agent.prompts import build_system_prompt
    prompt = build_system_prompt(ws, "", "policy", task="build a matmul engine")
    assert "DECIDE THE ARCHITECTURE FIRST" in prompt
    assert "tree" in prompt.lower() and "top-down" in prompt.lower()
    assert "every node" in prompt.lower() or "EVERY node" in prompt
    assert "whole sub-tree" in prompt.lower() or "whole subtree" in prompt.lower()
    assert "any level" in prompt.lower()  # leaf at any depth, match at any level
    assert "stop descending" in prompt.lower()  # prune the branch on a match


# ---- live corpus ----------------------------------------------------------------


LIVE = Path(os.path.expanduser("~/rtllib-pillars"))


@pytest.mark.skipif(not (LIVE / "manifest.json").is_file(),
                    reason="~/rtllib-pillars not present")
def test_live_rtllib_pillars_corpus(tmp_path):
    ws = _mk_ws(tmp_path / "work",
                config_text=f"[project]\nname='t'\n[library]\ndirs=['{LIVE}']\n")
    rep = lib.discover_report(ws)
    assert rep.libraries and rep.libraries[0]["name"] == "rtllib-pillars"
    assert len(rep.modules) >= 100
    hits = lib.search(ws, query="async fifo cdc")
    assert any("fifo" in m.name for m in hits)
    card = lib.module_card(rep.modules["fifo_sync"])
    assert card["params"] and card["files"]
    for f in card["files"]:
        assert Path(f["src"]).is_file()
