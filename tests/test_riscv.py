"""RISC-V vertical: toolchain discovery/build, trace normalization, and the
first-divergence diff. The trace/diff half is pure and always runs; the build
and reference-model half needs a riscv gcc and skips cleanly without one."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.riscv.toolchain import Toolchain, default_abi, discover_toolchain
from chipchamp.riscv.trace import (TraceStep, diff_traces, parse_gdbsim_trace,
                                   parse_rtl_trace, parse_spike_trace,
                                   parse_trace)
from chipchamp.tools import all_tools


# ---- trace parsing ------------------------------------------------------------------


def test_parse_gdbsim_trace():
    text = ("insn:     0x010000                       -addi ra, zero, 0x5;  "
            "// ra = zero + 0x5\n"
            "insn:     0x010004                       -add gp, ra, sp;\n"
            "some other line\n")
    steps = parse_gdbsim_trace(text)
    assert [s.pc for s in steps] == [0x10000, 0x10004]
    assert steps[0].disasm == "addi ra, zero, 0x5"


def test_parse_spike_trace_with_writeback():
    text = ("core   0: 0x0000000080000000 (0x00000297) auipc t0, 0x0\n"
            "core   0: 3 0x0000000080000004 (0x00500093) x1  0x0000000000000005\n")
    steps = parse_spike_trace(text)
    assert [s.pc for s in steps] == [0x80000000, 0x80000004]
    assert steps[1].reg == "x1" and steps[1].value == 5


def test_parse_rtl_trace_is_permissive():
    """A team shouldn't have to adopt a trace format to get value from cosim."""
    text = ("[TRACE] pc=0x80000004 instr=0x00500093 rd=1\n"
            "cycle 12: PC: 0x80000008  INSN:0x002081b3\n"
            "unrelated log line\n")
    steps = parse_rtl_trace(text)
    assert [s.pc for s in steps] == [0x80000004, 0x80000008]
    assert steps[0].insn == 0x00500093


def test_parse_trace_autodetects():
    _, kind = parse_trace("insn:     0x010000    -addi ra, zero, 0x5;\n")
    assert kind == "gdbsim"
    _, kind = parse_trace("core   0: 0x0000000080000000 (0x00000297) auipc t0, 0x0\n")
    assert kind == "spike"
    _, kind = parse_trace("[TRACE] pc=0x80000004\n")
    assert kind == "rtl"
    assert parse_trace("nothing useful\n")[1] == "unknown"


# ---- the diff -------------------------------------------------------------------------


def _steps(*pcs):
    return [TraceStep(pc=p, index=i) for i, p in enumerate(pcs)]


def test_diff_finds_the_first_pc_divergence():
    dut = _steps(0x1000, 0x1004, 0x1008, 0x1040)
    ref = _steps(0x1000, 0x1004, 0x1008, 0x100c)
    d = diff_traces(dut, ref)
    assert d and d.reason == "pc" and d.index == 3
    assert "0x00001040" in d.describe() and "0x0000100c" in d.describe()


def test_diff_reports_the_first_divergence_not_a_later_one():
    """Re-syncing after a divergence would hide the actual bug."""
    dut = _steps(0x1000, 0x2000, 0x1008, 0x3000)
    ref = _steps(0x1000, 0x1004, 0x1008, 0x100c)
    assert diff_traces(dut, ref).index == 1


def test_diff_catches_instruction_and_writeback_mismatch():
    dut = [TraceStep(pc=0x1000, insn=0x13), TraceStep(pc=0x1004, insn=0x99)]
    ref = [TraceStep(pc=0x1000, insn=0x13), TraceStep(pc=0x1004, insn=0x93)]
    assert diff_traces(dut, ref).reason == "insn"
    dut2 = [TraceStep(pc=0x1000, reg="x1", value=5)]
    ref2 = [TraceStep(pc=0x1000, reg="x1", value=6)]
    assert diff_traces(dut2, ref2).reason == "writeback"


def test_a_prefix_is_a_match_not_a_divergence():
    """The reference is usually cut short at a step bound (bare-metal programs
    spin forever) — a shorter but agreeing core trace must not read as a bug."""
    dut = _steps(0x1000, 0x1004)
    ref = _steps(0x1000, 0x1004, 0x1008, 0x100c)
    assert diff_traces(dut, ref) is None
    d = diff_traces(dut, ref, strict_length=True)
    assert d and d.reason == "length"


def test_diff_skips_a_boot_stub():
    dut = _steps(0xF0, 0xF4, 0x1000, 0x1004)
    ref = _steps(0xAA, 0xBB, 0x1000, 0x1004)
    assert diff_traces(dut, ref) is not None          # differs in the stub
    assert diff_traces(dut, ref, skip=2) is None      # ignore it


def test_identical_traces_match():
    assert diff_traces(_steps(1, 2, 3), _steps(1, 2, 3)) is None


# ---- toolchain ------------------------------------------------------------------------


def test_default_abi_follows_the_isa():
    assert default_abi("rv32i") == "ilp32"
    assert default_abi("rv32imf") == "ilp32f"
    assert default_abi("rv64imafdc") == "lp64d"
    assert default_abi("rv64i") == "lp64"


def test_multilib_sentinel_is_parsed():
    """`-print-multi-lib` prints '.;' for a single-variant toolchain; comparing
    against '.' misread it as a multilib list and rejected every ISA."""
    tc = Toolchain(prefix="x-", multilibs=[".;"], default_march="rv64imafdc")
    assert tc.has_multilib("rv64i")        # same XLEN as the only variant
    assert not tc.has_multilib("rv32i")    # would need rv32 libraries
    tc2 = Toolchain(prefix="x-", multilibs=["rv32i/ilp32;@march=rv32i@mabi=ilp32"])
    assert tc2.has_multilib("rv32i")


def test_missing_toolchain_degrades():
    tc = discover_toolchain("/nonexistent/prefix-")
    assert not tc.available or tc.prefix   # never raises


# ---- live build + reference model -------------------------------------------------------

_HAS_GCC = discover_toolchain().available
requires_riscv = pytest.mark.skipif(not _HAS_GCC, reason="no riscv gcc")


def _ref_base() -> str:
    """Link address the ACTIVE reference model can actually load: spike's DRAM
    starts at 0x80000000; the gdb simulator maps low addresses instead."""
    from chipchamp.riscv.toolchain import find_iss
    backend, _ = find_iss(discover_toolchain())
    return "0x80000000" if backend == "spike" else "0x1000"

_ASM = """\
    .section .text.init
    .globl _start
_start:
    li   x1, 5
    li   x2, 7
    add  x3, x1, x2
1:  j 1b
"""


@requires_riscv
def test_compile_and_reference_run(ctx, tmp_path):
    src = os.path.join(str(ctx.ws.root), "sw", "_t_riscv.S")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "w") as fh:
        fh.write(_ASM)
    try:
        c = all_tools()["riscv.compile"].handler(
            ctx, sources=["sw/_t_riscv.S"], name="_t_riscv", march="rv32i",
            base=_ref_base())
        assert "error" not in c, c
        elf = os.path.join(str(ctx.ws.root), c["elf"])
        assert os.path.exists(elf)
        assert c["march"] == "rv32i" and c["mabi"] == "ilp32"
        assert c["words"] >= 4                      # $readmemh image written
        assert "_start" in c["symbols"]
        assert "add" in c["disassembly"]

        r = all_tools()["riscv.refrun"].handler(ctx, elf=c["elf"], max_steps=20,
                                                timeout=20)
        if r.get("error"):
            pytest.skip(f"reference model unusable here: {r['error']}")
        assert r["steps"] > 0
        # the program ends in a spin loop, so the run must TERMINATE either way:
        # bounded by our step/time cap, or by the model's own instruction limit
        assert isinstance(r["bounded"], bool)
        pcs = [h["pc"] for h in r["head"]]
        assert f"0x{int(_ref_base(), 16):08x}" in pcs   # our _start was executed
    finally:
        if os.path.exists(src):
            os.remove(src)


@requires_riscv
def test_cosim_reports_first_divergence_against_the_reference(ctx):
    """End-to-end: a core trace that takes a wrong branch is localized to the
    instruction, with the reference's disassembly."""
    src = os.path.join(str(ctx.ws.root), "sw", "_t_cosim.S")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "w") as fh:
        fh.write(_ASM)
    good = os.path.join(str(ctx.ws.root), "sw", "_t_good.log")
    bad = os.path.join(str(ctx.ws.root), "sw", "_t_bad.log")
    try:
        c = all_tools()["riscv.compile"].handler(
            ctx, sources=["sw/_t_cosim.S"], name="_t_cosim", march="rv32i",
            base=_ref_base())
        assert "error" not in c, c
        # align to where the model actually starts executing OUR code: spike
        # runs a bootrom first, so the core's stream is compared after it
        run = all_tools()["riscv.refrun"].handler(ctx, elf=c["elf"],
                                                  max_steps=20, timeout=25)
        if run.get("error"):
            pytest.skip(f"reference model unusable here: {run['error']}")
        ref_pcs = [int(h["pc"], 16) for h in run["head"]]
        start = int(_ref_base(), 16)
        skip_n = ref_pcs.index(start)          # bootrom length (0 for gdbsim)
        # cosim should find this offset ITSELF — the caller shouldn't have to
        # know how long a given model's bootrom is
        with open(good, "w") as fh:
            fh.write("".join(f"pc=0x{p:08x}\n" for p in ref_pcs[skip_n:skip_n + 4]))
        with open(bad, "w") as fh:
            fh.write("".join(f"pc=0x{p:08x}\n" for p in
                             (ref_pcs[skip_n], ref_pcs[skip_n + 1], 0x1040)))
        T = all_tools()["riscv.cosim"].handler
        ok = T(ctx, core_trace="sw/_t_good.log", elf=c["elf"], max_steps=20)
        assert ok["match"] is True, ok
        assert ok["skipped_reference"] == skip_n     # auto-aligned past the bootrom
        ng = T(ctx, core_trace="sw/_t_bad.log", elf=c["elf"], max_steps=20)
        assert ng["match"] is False, ng
        assert ng["divergence"]["core"]["pc"] == "0x00001040"
    finally:
        for p in (src, good, bad):
            if os.path.exists(p):
                os.remove(p)


def test_tools_registered():
    names = set(all_tools())
    assert {"riscv.toolchain", "riscv.compile", "riscv.refrun",
            "riscv.cosim"} <= names


def test_toolchain_tool_reports_absence_cleanly(ctx, monkeypatch):
    monkeypatch.setattr("chipchamp.riscv.toolchain.shutil.which", lambda *_: None)
    r = all_tools()["riscv.toolchain"].handler(ctx)
    assert r["available"] is False and "hint" in r


# ---- spike commit log ------------------------------------------------------------


def test_spike_log_commits_is_not_double_counted():
    """--log-commits emits TWO lines per instruction (disassembly, then the
    architectural writeback). Counting both would double the stream and desync
    lockstep alignment against a core that retires each instruction once."""
    text = ("core   0: 0x80000000 (0x00500093) li      ra, 5\n"
            "core   0: 3 0x80000000 (0x00500093) x1  0x0000000000000005\n"
            "core   0: 0x80000004 (0x00700113) li      sp, 7\n"
            "core   0: 3 0x80000004 (0x00700113) x2  0x0000000000000007\n")
    steps = parse_spike_trace(text)
    assert len(steps) == 2                      # not 4
    assert steps[0].pc == 0x80000000 and steps[0].disasm.startswith("li")
    assert steps[0].reg == "x1" and steps[0].value == 5
    assert steps[1].reg == "x2" and steps[1].value == 7


def test_writeback_divergence_is_caught_when_both_sides_report_it():
    dut = [TraceStep(pc=0x80000000, insn=0x93, reg="x1", value=4)]
    ref = [TraceStep(pc=0x80000000, insn=0x93, reg="x1", value=5)]
    d = diff_traces(dut, ref)
    assert d and d.reason == "writeback" and "x1" in d.describe()


# ---- compliance (RISCOF) -----------------------------------------------------------


def test_parse_riscof_log_real_shape():
    """RISCOF logs one line per test through colorlog; that is a stabler signal
    than scraping its HTML report."""
    from chipchamp.riscv.compliance import parse_riscof_log
    log = ("\x1b[32m    INFO\x1b[0m | Following 3 tests have been run :\n"
           "\x1b[32m    INFO\x1b[0m | TEST NAME      : COMMIT ID  : STATUS\n"
           "\x1b[32m    INFO\x1b[0m | add-01.S       : 2ac7d9f    : Passed\n"
           "\x1b[32m    INFO\x1b[0m | sll-01.S       : 2ac7d9f    : Passed\n"
           "\x1b[31m   ERROR\x1b[0m | csrrw-01.S     : 2ac7d9f    : Failed\n")
    r = parse_riscof_log(log)
    assert r["total"] == 3 and r["passed"] == 2 and r["failed"] == 1
    assert r["failures"] == ["csrrw-01.S"]
    assert r["reported_total"] == 3          # cross-check against RISCOF's own count
    # the header row must not be mistaken for a test
    assert all(t["name"] != "TEST" for t in r["tests"])


def test_riscof_argv_shape():
    from chipchamp.riscv.compliance import riscof_argv
    argv = riscof_argv("/x/riscof", suite="/s", env="/s/env", config="/c.ini",
                       work_dir="/w")
    assert argv[:2] == ["/x/riscof", "run"]
    assert "--no-browser" in argv and "--suite" in argv and "--env" in argv


def test_guess_suite(tmp_path):
    from chipchamp.riscv.compliance import guess_suite
    assert guess_suite(str(tmp_path)) == ("", "")
    (tmp_path / "riscv-arch-test" / "riscv-test-suite" / "env").mkdir(parents=True)
    s, e = guess_suite(str(tmp_path))
    assert s.endswith("riscv-test-suite") and e.endswith("env")


def test_compliance_without_riscof_is_a_clear_error(ctx, monkeypatch):
    monkeypatch.setattr("chipchamp.riscv.compliance.shutil.which", lambda *_: None)
    r = all_tools()["riscv.compliance"].handler(ctx)
    assert "error" in r and "riscof" in r["error"] and "hint" in r


# ---- task class + rung R -------------------------------------------------------------


def test_riscv_task_class_only_for_a_declared_core():
    from chipchamp.policy.classify import classify
    from chipchamp.policy.diff import Diff, FileChange
    d = Diff(files=[FileChange(path="rtl/core.sv", status="modified",
                               old_text="module core; endmodule",
                               new_text="module core; wire x; endmodule")])
    assert classify(d) == "rtl_functional"          # ordinary project
    assert classify(d, riscv_core=True) == "riscv"  # workspace declares [riscv]
    # a docs-only change stays docs even for a core
    d2 = Diff(files=[FileChange(path="README.md", status="modified",
                                old_text="a", new_text="b")])
    assert classify(d2, riscv_core=True) == "docs"


def test_rung_r_keeps_functional_rigor_and_adds_isa_gates():
    from chipchamp.policy.gates import CLASS_GATES, evaluate
    from chipchamp.policy import Evidence
    rung, gates = CLASS_GATES["riscv"]
    assert rung == "R"
    # selecting `riscv` must not trade away ordinary RTL rigor
    assert {"lint", "smoke_sim", "affected_regress", "coverage_baseline",
            "no_gaming"} <= set(gates)
    assert {"isa_cosim", "isa_compliant"} <= set(gates)
    rep = evaluate("riscv", Evidence(jobs=[]))
    assert "isa_cosim" in rep.blocking and "isa_compliant" in rep.blocking


def test_isa_gates_transition_missing_fail_pass():
    from chipchamp.policy import Evidence
    from chipchamp.policy.gates import evaluate

    def gate(ev, name):
        return next(g for g in evaluate("riscv", ev).gates if g.name == name)

    ev = Evidence(jobs=[])
    assert gate(ev, "isa_cosim").status == "missing"
    ev.riscv_cosim = {"match": False, "compared": 40,
                      "summary": "diverged at #7", "divergence": {"index": 7}}
    assert gate(ev, "isa_cosim").status == "fail"
    ev.riscv_cosim = {"match": True, "compared": 0}
    assert gate(ev, "isa_cosim").status == "fail"      # vacuous match is not evidence
    ev.riscv_cosim = {"match": True, "compared": 120, "partial": True}
    g = gate(ev, "isa_cosim")
    assert g.status == "pass" and "PREFIX" in g.detail  # honest about depth

    assert gate(ev, "isa_compliant").status == "missing"
    ev.riscv_compliance = {"total": 0, "passed": 0, "failed": 0}
    assert gate(ev, "isa_compliant").status == "fail"  # an empty suite proves nothing
    ev.riscv_compliance = {"total": 3, "passed": 2, "failed": 1,
                           "failures": ["csrrw-01.S"]}
    assert gate(ev, "isa_compliant").status == "fail"
    ev.riscv_compliance = {"total": 55, "passed": 55, "failed": 0}
    assert gate(ev, "isa_compliant").status == "pass"


def test_cosim_records_evidence_on_the_context(ctx, tmp_path):
    """The gate reads a machine verdict off the context — the model cannot
    assert conformance in prose."""
    ref = tmp_path / "ref.trace"
    dut = tmp_path / "dut.log"
    ref.write_text("".join(f"insn:     0x{p:08x}       -nop;\n"
                           for p in (0x1000, 0x1004, 0x1008)))
    dut.write_text("".join(f"pc=0x{p:08x}\n" for p in (0x1000, 0x1004, 0x1008)))
    ctx.riscv_cosim = None
    r = all_tools()["riscv.cosim"].handler(
        ctx, core_trace=str(dut), reference_trace=str(ref))
    assert r["match"] is True
    assert ctx.riscv_cosim and ctx.riscv_cosim["match"] is True
    assert ctx.riscv_cosim["compared"] == 3


def test_align_traces_finds_the_model_bootrom():
    """spike executes a bootrom (0x1000…) that jumps to the ELF entry; the core
    never runs it. Alignment must be found automatically."""
    from chipchamp.riscv.trace import align_traces
    ref = _steps(0x1000, 0x1004, 0x1008, 0x100c, 0x1010,
                 0x80000000, 0x80000004)
    dut = _steps(0x80000000, 0x80000004)
    assert align_traces(dut, ref) == 5
    assert diff_traces(dut, ref, skip_ref=5) is None
    # no common start → no alignment invented
    assert align_traces(_steps(0xdead), ref) == 0


def test_skip_is_per_side_so_a_bootrom_does_not_erase_the_comparison():
    """A single `skip` applied to BOTH streams would drop the core's real
    instructions and vacuously 'match' by comparing nothing."""
    ref = _steps(0x1000, 0x1004, 0x1008, 0x100c, 0x1010, 0x80000000, 0x80000004)
    dut = _steps(0x80000000, 0x99999999)          # second instruction is wrong
    assert diff_traces(dut, ref, skip=5) is None  # legacy both-sides: compares nothing
    d = diff_traces(dut, ref, skip_ref=5)         # per-side: the bug is caught
    assert d and d.reason == "pc" and d.index == 1


# ---- barrel multithreading -----------------------------------------------------------


def test_hart_is_captured_from_both_producers():
    assert parse_spike_trace("core   1: 0x80000000 (0x00500093) li ra, 5\n")[0].hart == 1
    assert parse_rtl_trace("[WB] tid=2 pc=0x00000004\n")[0].hart == 2
    assert parse_rtl_trace("thread=3 pc=0x8 instr=0x13\n")[0].hart == 3
    assert parse_rtl_trace("pc=0x8\n")[0].hart == 0          # single-threaded


def test_split_by_hart_demuxes_a_barrel_stream():
    """A barrel core round-robins T threads into ONE retired stream; each
    thread's own program order is what compares against the model."""
    from chipchamp.riscv.trace import split_by_hart
    steps = parse_rtl_trace("".join(
        f"pc=0x{pc:08x} tid={t}\n" for pc in (0, 4, 8) for t in range(4)))
    by = split_by_hart(steps)
    assert sorted(by) == [0, 1, 2, 3]
    assert [s.pc for s in by[2]] == [0, 4, 8]     # thread order, de-interleaved


def test_spike_writeback_fold_does_not_cross_harts():
    text = ("core   0: 0x80000000 (0x00500093) li ra, 5\n"
            "core   1: 3 0x80000000 (0x00500093) x1  0x0000000000000005\n")
    steps = parse_spike_trace(text)
    assert len(steps) == 2                        # different harts: not folded
    assert steps[0].hart == 0 and steps[1].hart == 1


def test_cosim_isolates_the_faulty_thread(ctx, tmp_path):
    """The whole point for a barrel core: one bad thread must be named, not
    hidden behind an overall verdict."""
    ref = tmp_path / "ref.trace"
    ref.write_text("".join(f"insn:     0x{p:08x}       -nop;\n"
                           for p in (0x0, 0x4, 0x8, 0xc)))
    dut = tmp_path / "dut.log"
    dut.write_text("".join(
        f"[WB] tid={t} pc=0x{(0xABC if (t == 2 and i == 3) else pc):08x}\n"
        for i, pc in enumerate((0x0, 0x4, 0x8, 0xc)) for t in range(4)))
    r = all_tools()["riscv.cosim"].handler(
        ctx, core_trace=str(dut), reference_trace=str(ref), max_steps=8)
    assert r["match"] is False
    assert r["threads_compared"] == 4
    assert r["harts"][2]["match"] is False
    assert all(r["harts"][h]["match"] for h in (0, 1, 3))
    assert "0x00000abc" in r["summary"]


def test_max_steps_is_a_per_hart_budget(ctx, tmp_path):
    """Regression: capping the RAW parse truncated the later threads — a 4-way
    core emits 4 lines per issue slot, so a bug just past the cut vanished and
    every thread reported a false match."""
    ref = tmp_path / "ref.trace"
    ref.write_text("".join(f"insn:     0x{p:08x}       -nop;\n"
                           for p in (0x0, 0x4, 0x8, 0xc)))
    dut = tmp_path / "dut.log"
    dut.write_text("".join(
        f"[WB] tid={t} pc=0x{(0xABC if (t == 3 and i == 3) else pc):08x}\n"
        for i, pc in enumerate((0x0, 0x4, 0x8, 0xc)) for t in range(4)))
    # 4 instructions per thread; a raw cap of 4 lines would see only thread 0's
    # first instruction and miss the divergence entirely
    r = all_tools()["riscv.cosim"].handler(
        ctx, core_trace=str(dut), reference_trace=str(ref), max_steps=4)
    assert r["match"] is False and r["harts"][3]["match"] is False


def test_cosim_can_target_one_hart(ctx, tmp_path):
    ref = tmp_path / "ref.trace"
    ref.write_text("".join(f"insn:     0x{p:08x}       -nop;\n" for p in (0, 4, 8)))
    dut = tmp_path / "dut.log"
    dut.write_text("".join(f"pc=0x{pc:08x} tid={t}\n"
                           for pc in (0, 4, 8) for t in (0, 1)))
    r = all_tools()["riscv.cosim"].handler(
        ctx, core_trace=str(dut), reference_trace=str(ref), hart=1)
    assert r["match"] is True and r["threads_compared"] == 1
    bad = all_tools()["riscv.cosim"].handler(
        ctx, core_trace=str(dut), reference_trace=str(ref), hart=9)
    assert "error" in bad and "hart 9" in bad["error"]


def test_hart_id_is_decimal_unless_hex_prefixed():
    """Found on a real barrel core: a testbench prints the thread with %0d, so
    parsing it as hex silently renumbered threads 10-15 as 16-21 — the trace
    then reported 22 threads on a 16-thread machine."""
    assert parse_rtl_trace("pc=0x00000010 tid=13\n")[0].hart == 13
    assert parse_rtl_trace("pc=0x10 tid=15\n")[0].hart == 15
    assert parse_rtl_trace("pc=0x10 hart=0x0f\n")[0].hart == 15   # explicit hex
    ids = {s.hart for s in parse_rtl_trace(
        "".join(f"pc=0x0 tid={t}\n" for t in range(16)))}
    assert ids == set(range(16))


def test_pc_offset_compares_across_memory_maps():
    """A core and a model often cannot share a memory map: spike page-aligns
    memory to 4 KiB and its debug module owns 0x0-0x1000, so a core booting at
    0x0 is compared against a model image built at another base."""
    from chipchamp.riscv.trace import align_traces
    dut = _steps(0x0, 0x4, 0x8)
    ref = _steps(0x80000000, 0x80000004, 0x80000008)
    assert diff_traces(dut, ref) is not None                 # raw: everything differs
    assert diff_traces(dut, ref, pc_offset=0x80000000) is None
    assert align_traces(dut, ref, pc_offset=0x80000000) == 0
    bad = _steps(0x0, 0x4, 0x40)
    d = diff_traces(bad, ref, pc_offset=0x80000000)
    assert d and d.index == 2 and "offset" in d.describe()


def test_spike_multi_hart_argv():
    from chipchamp.riscv.toolchain import iss_argv
    argv = iss_argv("spike", "/x/spike", "a.elf", march="rv32i", harts=16)
    assert "-p16" in argv
    assert "-p1" not in iss_argv("spike", "/x/spike", "a.elf", harts=1)


def test_gdbsim_rv32_arithmetic_warning(ctx, monkeypatch, tmp_path):
    """The gdb simulator does not truncate 32-bit arithmetic: measured against
    spike and against real hardware, an rv32ui add overflow case takes the
    test's fail branch. Using it as an RV32 reference must carry that warning."""
    import chipchamp.tools.riscv_tools as rt
    elf = tmp_path / "x.elf"
    elf.write_text("")
    monkeypatch.setattr(rt, "_tc", lambda c: __import__(
        "chipchamp.riscv.toolchain", fromlist=["x"]).Toolchain(prefix="p-"))
    monkeypatch.setattr("chipchamp.riscv.toolchain.Toolchain.available", True)
    monkeypatch.setattr("chipchamp.riscv.toolchain.find_iss",
                        lambda tc, prefer="": ("gdbsim", "/x/run"))
    monkeypatch.setattr("chipchamp.riscv.toolchain.isa_for_elf",
                        lambda tc, p: "rv32imafdc_zicsr_zifencei")
    monkeypatch.setattr("chipchamp.riscv.toolchain.run_iss_bounded",
                        lambda argv, **k: ("insn:     0x00000000  -nop;\n", False, False))
    r = rt.riscv_refrun(ctx, elf=str(elf))
    assert r["steps"] == 1
    assert "warning" in r and "spike" in r["warning"]
