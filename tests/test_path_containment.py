"""Every model-supplied path is canonicalised and contained before a tool
touches it. The policy engine matched globs on the raw string, so
`./waivers/x.yaml` slipped past `waivers/**` and `rtl/../.chipchamp/policy.yaml`
past the protected-path guard; fs.read passed absolute paths straight
through; and fpga.pins, uvm.scaffold, regmap.generate, lib.fetch and
checkpoint.restore wrote wherever they were pointed."""
from __future__ import annotations

import os

import pytest

from chipchamp.policy.engine import canonical
from chipchamp.tools import all_tools
from chipchamp.tools.paths import PathRefused, safe_name, ws_path


# ---- canonical form ---------------------------------------------------------

def test_canonical_collapses_dots_and_keeps_escapes_visible():
    assert canonical("./waivers/a.yaml") == "waivers/a.yaml"
    assert canonical("rtl/../.chipchamp/policy.yaml") == ".chipchamp/policy.yaml"
    assert canonical("rtl//gen/./x.sv") == "rtl/gen/x.sv"
    assert canonical("../x") == "../x"
    assert canonical("/abs/p").startswith("/")
    assert canonical(".") == ""


def test_policy_sees_the_canonical_path(ctx):
    pol = ctx.policy
    for p in ("./.chipchamp/policy.yaml", "rtl/../.chipchamp/policy.yaml",
              "./waivers/x.yaml", "waivers/../waivers/x.yaml"):
        ok, why = pol.can_write(p)
        assert not ok and "protected" in why, p
    ok, why = pol.can_write("./rtl/gen/timer_reg_top.sv")
    assert not ok and "GENERATED" in why
    ok, _ = pol.can_write("./rtl/gen/timer_reg_top.sv", allow_generated=True)
    assert ok                                       # the sanctioned generator
    ok, why = pol.can_write("rtl/gen/../../waivers/x.yaml", allow_generated=True)
    assert not ok and "protected" in why            # ...but nothing else
    for p in ("/etc/hosts", "../outside.sv", ".."):
        ok, why = pol.can_write(p)
        assert not ok and "outside the workspace" in why, p


# ---- the shared resolver ----------------------------------------------------

def test_ws_path_contains_and_teaches(ctx):
    ap, rel = ws_path(ctx, "./rtl/soc_top.sv")
    assert rel == "rtl/soc_top.sv" and ap.startswith(os.path.realpath(ctx.ws.root))
    with pytest.raises(PathRefused) as e:
        ws_path(ctx, "/etc/hostname")
    assert "workspace-relative" in e.value.payload["error"]
    with pytest.raises(PathRefused) as e:
        ws_path(ctx, "rtl/../../outside")
    assert "escapes the workspace" in e.value.payload["error"]
    with pytest.raises(PathRefused) as e:
        ws_path(ctx, "./waivers/w.yaml", write=True)
    assert "write denied" in e.value.payload["error"]


def test_safe_name_rejects_path_components():
    assert safe_name("arty-a7") == "arty-a7" and safe_name("J-0004") == "J-0004"
    for bad in ("", ".", "..", "../evil", "a/b", "J-0001/../x", "x\\y"):
        assert safe_name(bad) is None, bad


# ---- read side --------------------------------------------------------------

def test_fs_read_list_grep_are_contained(ctx):
    from chipchamp.tools.fs_tools import fs_grep, fs_list, fs_read
    assert "workspace-relative" in fs_read(ctx, "/etc/hostname")["error"]
    assert "escapes the workspace" in fs_read(ctx, "../../etc/hostname")["error"]
    assert fs_read(ctx, "./rtl/soc_top.sv")["path"] == "rtl/soc_top.sv"
    assert "escapes the workspace" in fs_list(ctx, "..")["error"]
    assert "workspace-relative" in fs_list(ctx, "/")["error"]
    assert "files" in fs_list(ctx, "./rtl")
    assert "workspace-relative" in fs_grep(ctx, "x", glob="/etc/*")["error"]
    assert "escapes the workspace" in fs_grep(ctx, "x", glob="../*/*.sv")["error"]
    assert "hits" in fs_grep(ctx, "module", glob="rtl/*.sv")


# ---- the five tools that wrote outside the guard ----------------------------

def test_fpga_pins_is_a_write_tool_and_contained(ctx, tmp_path):
    from chipchamp.tools.fpga_tools import fpga_pins
    assert all_tools()["fpga.pins"].permission == "write"
    out = fpga_pins(ctx, action="import", file="/etc/hostname")
    assert "workspace-relative" in out["error"]
    out = fpga_pins(ctx, action="generate", module="counter", board="x",
                    file="../../evil.xdc", write=True)
    assert "escapes the workspace" in out["error"]


def test_fpga_pins_import_rejects_a_board_name_that_is_a_path(tmp_path):
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    from chipchamp.tools.fpga_tools import fpga_pins
    (tmp_path / ".chipchamp").mkdir()
    (tmp_path / "pins.xdc").write_text(
        'set_property -dict { PACKAGE_PIN H5 IOSTANDARD LVCMOS33 } [get_ports { led }];\n')
    c = ToolContext(Workspace(str(tmp_path)))
    out = fpga_pins(c, action="import", file="pins.xdc", board="../evil")
    assert "plain name" in out["error"]
    assert not (tmp_path.parent / "evil.json").exists()


def test_uvm_scaffold_refuses_an_absolute_or_escaping_out_dir(ctx, tmp_path):
    from chipchamp.tools.uvm_tools import uvm_scaffold
    out = uvm_scaffold(ctx, "counter", out_dir=str(tmp_path / "evil"), add_test=False)
    assert "workspace-relative" in out["error"]
    out = uvm_scaffold(ctx, "counter", out_dir="../evil", add_test=False)
    assert "escapes the workspace" in out["error"]
    assert not (tmp_path / "evil").exists()


def test_regmap_tools_refuse_specs_and_outputs_outside(ctx, tmp_path):
    from chipchamp.tools.regmap_tools import regmap_generate, regmap_validate
    assert "workspace-relative" in regmap_validate(ctx, str(tmp_path / "s.yaml"))["error"]
    assert "escapes the workspace" in regmap_generate(ctx, "../s.yaml")["error"]


def test_lib_fetch_refuses_an_escaping_dest(ctx):
    from chipchamp.library import discover
    from chipchamp.tools.lib_tools import lib_fetch
    mods = discover(ctx.ws)
    if not mods:
        pytest.skip("no library registered in the example workspace")
    name = sorted(mods)[0]
    out = lib_fetch(ctx, name, dest="../../outside")
    assert "escapes the workspace" in out["error"]


def test_checkpoint_restore_rejects_a_job_id_that_is_a_path(ctx):
    from chipchamp.tools.meta_tools import checkpoint_restore
    for bad in ("../../etc", "J-0001/../x", "/tmp"):
        out = checkpoint_restore(ctx, bad)
        assert "not a job id" in out["error"], bad
