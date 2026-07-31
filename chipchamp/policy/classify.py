"""Task classification (SPEC §11.1): conservative — highest applicable class wins.

Maps a change set to a task class, which in turn dictates the minimum verification
ladder and the mandatory gates. Because classification drives *what must pass
before "done"*, it errs toward the stricter class when signals conflict.
"""
from __future__ import annotations

from .diff import Diff

# ordered from least to most demanding; index used to pick the max
TASK_CLASSES = [
    "docs",
    "tb_only",
    "build_scripts",
    "regmap",
    "rtl_nfc",
    "rtl_functional",
    "riscv",
    "timing",
    "cdc",
    "physical",
    "efpga-fabulous",
    "fpga",
]
_RANK = {c: i for i, c in enumerate(TASK_CLASSES)}


def classify(diff: Diff, generated_globs: list[str] | None = None,
             riscv_core: bool = False) -> str:
    """`generated_globs` (SPEC §12.5): files matching the source-of-truth map's
    outputs are regenerated artifacts — they classify as `regmap` (their spec is
    the real change), not as hand-edited RTL.

    `riscv_core` (the workspace declares `[riscv]`): editing the RTL of a core
    that claims an ISA puts ISA conformance inside "done", so such a change
    classifies as `riscv` rather than plain `rtl_functional`."""
    generated_globs = generated_globs or []
    candidates: list[str] = []
    has_rtl = any(f.is_rtl and not f.in_globs(generated_globs) for f in diff.files)
    for f in diff.files:
        parts = f.path.replace("\\", "/").split("/")
        if f.path.endswith((".sdc", ".pdn.tcl")) or "floorplan" in f.path.lower():
            candidates.append("physical")  # SDC/floorplan/PDN → needs re-signoff
        elif f.path.endswith((".xdc", ".pcf", ".lpf")):
            candidates.append("fpga")  # FPGA constraints → re-implement
        elif "user_design" in parts or parts[-1] == "fabric.csv" \
                or ("Tile" in parts and f.path.endswith((".csv", ".list"))):
            # FABulous project content: the design mapped ONTO a fabric, or the
            # fabric's own authoring files. The class existed in TASK_CLASSES
            # and its rung-E gates were fully implemented, but no branch here
            # ever produced it — so an eFPGA edit classified rtl_functional,
            # whose gates (smoke test, regression, coverage baseline) are
            # unpayable for a fabric-mapped design, and report.done could
            # never accept eFPGA work at all. Found writing the demo for it.
            candidates.append("efpga-fabulous")
        elif f.in_globs(generated_globs):
            candidates.append("regmap")
        elif f.is_doc:
            candidates.append("docs")
        elif f.is_regmap_source:
            candidates.append("regmap")
        elif f.is_build_script:
            candidates.append("build_scripts")
        elif f.is_tb:
            candidates.append("tb_only")
        elif f.is_rtl:
            candidates.append("rtl_functional")

    if has_rtl and riscv_core:
        # a core that claims an ISA must still execute it after the edit
        candidates.append("riscv")

    if has_rtl:
        # declared intents refine (or downgrade) the RTL class
        if diff.declared_cdc or _touches_domains(diff):
            candidates.append("cdc")
        if diff.declared_timing:
            candidates.append("timing")
        if diff.declared_nfc and "cdc" not in candidates and "timing" not in candidates:
            # NFC only downgrades if nothing forces functional
            candidates = [c for c in candidates if c != "rtl_functional"]
            candidates.append("rtl_nfc")

    if not candidates:
        return "docs"
    return max(candidates, key=lambda c: _RANK.get(c, 0))


def _touches_domains(diff: Diff) -> bool:
    """A change is CDC-relevant only if it touches a genuinely multi-clock module
    — i.e. the source clocks two or more distinct signals. (Substring matching on
    'sync' would false-positive on 'Synchronous'/'sync_fifo'.)"""
    import re
    for f in diff.files:
        if not f.is_rtl:
            continue
        clocks = set(re.findall(r"posedge\s+([A-Za-z_]\w*)", f.new_text)) - {"rst_n", "rst"}
        if len(clocks) >= 2:
            return True
    return False
