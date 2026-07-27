"""Regmap compiler + source-of-truth map (SPEC §9-I, §12.5, P10/US-09)."""
from __future__ import annotations

import os

from conftest import EXAMPLE, requires_icarus

from chipchamp.regmap import generate, load_spec, validate
from chipchamp.regmap.compiler import Field, Register, RegmapSpec
from chipchamp.tools import all_tools

SPEC = str(EXAMPLE / "hw" / "regs" / "timer.yaml")


def test_validate_catches_defects():
    s = RegmapSpec(name="bad", registers=[
        Register(name="A", offset=0x0, fields=[
            Field("x", 3, 0, "RW"), Field("y", 2, 2, "RW")]),   # overlap
        Register(name="A", offset=0x2, fields=[                  # dup name + misaligned
            Field("z", 0, 0, "FOO", reset=9)]),                  # bad access + wide reset
    ])
    errs = " | ".join(validate(s))
    for expected in ("overlaps", "duplicate register", "not word-aligned",
                     "access 'FOO'", "reset value wider"):
        assert expected in errs


def test_generation_is_deterministic():
    s = load_spec(SPEC)
    assert validate(s) == []
    a, b = generate(s), generate(s)
    assert a == b
    sv = a["timer_reg_top.sv"]
    assert "DO NOT EDIT" in sv and "hwif_status_ovf_set_i" in sv
    assert "0x0" not in a["timer_regs.h"] or "TIMER_CTRL_EN_MASK 0x1" in a["timer_regs.h"]


def test_generated_outputs_are_write_protected(ctx):
    """FR-PROJ-04: fs.write on a generated file is refused with a pointer to
    the source of truth."""
    r = all_tools()["fs.write"].handler(
        ctx, path="rtl/gen/timer_reg_top.sv", content="// vandalism")
    assert "error" in r
    assert "GENERATED" in r["error"]
    assert "hw/regs/timer.yaml" in r["error"]
    # the file on disk is untouched
    text = open(os.path.join(str(EXAMPLE), "rtl/gen/timer_reg_top.sv")).read()
    assert "vandalism" not in text


def test_regen_gate_detects_stale_outputs(ctx):
    from chipchamp.tools.regmap_tools import regen_check
    assert regen_check(ctx) is True  # committed outputs match the spec
    sv_path = os.path.join(str(EXAMPLE), "rtl/gen/timer_reg_top.sv")
    original = open(sv_path).read()
    try:
        with open(sv_path, "w") as fh:  # simulate a hand-edit behind the map's back
            fh.write(original + "\n// sneaky edit\n")
        assert regen_check(ctx) is False
    finally:
        with open(sv_path, "w") as fh:
            fh.write(original)


def test_regmap_change_classifies_as_regmap(ctx):
    from chipchamp.policy import Diff, FileChange
    d = Diff(files=[FileChange("hw/regs/timer.yaml", "modified"),
                    FileChange("rtl/gen/timer_reg_top.sv", "modified")])
    chk = ctx.policy.check(d)
    assert chk.task_class == "regmap"
    assert "regmap_regen" in chk.required_gates


@requires_icarus
def test_generated_block_passes_apb_testbench(ctx):
    r = all_tools()["sim.run"].handler(ctx, test="timer_regs_smoke", seed=1)
    assert r["sim_status"] == "pass"
