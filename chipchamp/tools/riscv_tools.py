"""RISC-V tools (SPEC §8.4 RISC-V vertical): build test programs, run a
reference model, and co-simulate a core against it.

``riscv.compile`` turns C/assembly into the ELF + ``$readmemh`` image +
disassembly a core testbench needs. ``riscv.refrun`` executes the same ELF on a
reference model (spike when installed, otherwise the toolchain's own
simulator). ``riscv.cosim`` compares the core's retired-instruction trace with
the reference's and reports the **first divergence** — PC, instruction and
disassembly — instead of "the test failed".
"""
from __future__ import annotations

import os
import subprocess

from .base import tool, truncate
from .context import ToolContext


def _workdir(ctx: ToolContext, name: str = "") -> str:
    d = os.path.join(str(ctx.ws.dot), "riscv", name) if name else \
        os.path.join(str(ctx.ws.dot), "riscv")
    os.makedirs(d, exist_ok=True)
    return d


def _abs(ctx: ToolContext, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ctx.ws.root, path)


def _tc(ctx: ToolContext):
    from ..riscv.toolchain import discover_toolchain
    cfg = (ctx.ws.config.get("riscv", {}) or {})
    return discover_toolchain(str(cfg.get("toolchain", "")))


@tool("riscv.toolchain",
      "What RISC-V software toolchain and reference model this machine has "
      "(gcc triple/version, linkable ISA variants, spike or the gdb simulator). "
      "Check this before assuming a core can be co-simulated.",
      group="riscv",
      schema={"type": "object", "properties": {}})
def riscv_toolchain(ctx: ToolContext) -> dict:
    from ..riscv.toolchain import find_iss
    tc = _tc(ctx)
    if not tc.available:
        return {"available": False,
                "error": "no riscv gcc found on PATH or in the usual prefixes",
                "hint": "install a bare-metal toolchain (riscv64-unknown-elf-gcc) "
                        "or set [riscv] toolchain = <prefix> in config"}
    backend, exe = find_iss(tc)
    return {"available": True, "triple": tc.triple, "version": tc.version,
            "prefix": tc.prefix, "default_march": tc.default_march,
            "default_mabi": tc.default_mabi,
            "multilibs": tc.multilibs or [],
            "reference_model": backend or None, "reference_exe": exe or None,
            "note": ("spike is the canonical reference model; the gdb simulator "
                     "ships with the toolchain and traces PCs, which is enough "
                     "for PC-lockstep" if backend == "gdbsim" else "")}


@tool("riscv.compile",
      "Compile C/assembly into a RISC-V ELF plus a $readmemh memory image and "
      "disassembly — the program a core testbench actually runs. `march` is the "
      "ISA contract under test (e.g. rv32i, rv32imc).",
      cost="cheap", permission="write", group="riscv",
      schema={"type": "object", "properties": {
          "sources": {"type": "array", "items": {"type": "string"},
                      "description": "workspace-relative .c/.S files"},
          "name": {"type": "string", "description": "output basename"},
          "march": {"type": "string", "default": "rv32i"},
          "mabi": {"type": "string", "description": "default: matches march"},
          "base": {"type": "string", "default": "0x80000000",
                   "description": "link/load address"},
          "ldscript": {"type": "string", "description": "custom linker script"},
          "hex_width": {"type": "integer", "default": 32,
                        "description": "memory word width for the hex image"},
          "opt": {"type": "string", "default": "-O2"},
          "freestanding": {"type": "boolean", "default": True,
                           "description": "bare-metal (-nostdlib): what a core "
                                          "testbench runs. Turn off to link "
                                          "newlib, which needs a matching "
                                          "multilib."}},
          "required": ["sources"]})
def riscv_compile(ctx: ToolContext, sources: list, name: str = "",
                  march: str = "rv32i", mabi: str = "", base: str = "0x80000000",
                  ldscript: str = "", hex_width: int = 32,
                  opt: str = "-O2", freestanding: bool = True) -> dict:
    from ..riscv.toolchain import (compile_argv, default_abi, disassemble,
                                   elf_symbols, elf_to_hex, write_linker_script)
    tc = _tc(ctx)
    if not tc.available:
        return {"error": "no RISC-V toolchain — run riscv.toolchain for details"}
    srcs = [_abs(ctx, s) for s in (sources or [])]
    missing = [ctx.rel(s) for s in srcs if not os.path.isfile(s)]
    if not srcs or missing:
        return {"error": f"source not found: {', '.join(missing) or '(none given)'}"}
    # Multilib only governs LINKING libc/libgcc. A freestanding build (the
    # default, and what a core testbench runs) needs none, so this blocks only
    # a hosted build that would fail at link.
    if not freestanding and not tc.has_multilib(march):
        return {"error": f"this toolchain has no {march} libraries to link",
                "multilibs": tc.multilibs or [],
                "hint": f"it links {tc.default_march or '?'} only — build "
                        f"freestanding (the default), or install a multilib "
                        f"toolchain"}
    name = name or os.path.splitext(os.path.basename(srcs[0]))[0]
    wd = _workdir(ctx, name)
    elf = os.path.join(wd, f"{name}.elf")
    lds = _abs(ctx, ldscript) if ldscript else write_linker_script(
        os.path.join(wd, "link.ld"), base=base)
    argv = compile_argv(tc, srcs, elf, march=march, mabi=mabi or default_abi(march),
                        ldscript=lds, opt=opt, freestanding=freestanding)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"compile failed to launch: {e}"}
    if r.returncode != 0:
        return {"error": "compilation failed",
                "diagnostics": (r.stderr or r.stdout).strip().splitlines()[-12:],
                "command": " ".join(argv[:6]) + " …"}
    out: dict = {"elf": ctx.rel(elf), "march": march,
                 "mabi": mabi or default_abi(march), "base": base,
                 "sources": [ctx.rel(s) for s in srcs]}
    try:
        hexfile = os.path.join(wd, f"{name}.hex")
        out["memory_image"] = ctx.rel(hexfile)
        out.update(elf_to_hex(tc, elf, hexfile, width=hex_width))
        out["base"] = f"0x{out.pop('base'):08x}" if isinstance(out.get("base"), int) \
            else out.get("base", base)
    except Exception as e:                       # hex is a convenience, not the build
        out["memory_image_error"] = str(e)[:200]
    syms = elf_symbols(tc, elf)
    out["symbols"] = {k: f"0x{v:08x}" for k, v in list(syms.items())[:40]}
    dis = os.path.join(wd, f"{name}.dis")
    text = disassemble(tc, elf, max_lines=100000)
    with open(dis, "w") as fh:
        fh.write(text)
    out["disassembly_file"] = ctx.rel(dis)
    out["disassembly"] = "\n".join(text.splitlines()[:40])
    if r.stderr.strip():
        out["warnings"] = r.stderr.strip().splitlines()[:8]
    return truncate(out)


@tool("riscv.refrun",
      "Execute an ELF on the RISC-V reference model and return its retired-"
      "instruction trace (normalized). This is the golden stream riscv.cosim "
      "compares a core against.", cost="metered", permission="submit",
      group="riscv",
      schema={"type": "object", "properties": {
          "elf": {"type": "string"},
          "max_steps": {"type": "integer", "default": 20000},
          "march": {"type": "string"},
          "region": {"type": "string",
                     "description": "gdbsim memory region ADDRESS,SIZE (a core's "
                                    "load address, e.g. 0x80000000,0x100000)"},
          "timeout": {"type": "number", "default": 60,
                      "description": "wall-clock bound; test programs usually "
                                     "end in a spin loop"},
          "harts": {"type": "integer", "default": 0,
                    "description": "spike: model this many harts (-pN). A "
                                   "barrel core's threads each have their own "
                                   "mhartid, so a program that branches on it "
                                   "needs one reference stream per hart."},
          "model": {"type": "string", "enum": ["auto", "spike", "gdbsim"],
                    "default": "auto",
                    "description": "spike is canonical BUT its debug module "
                                   "owns 0x0-0x1000, so a core booting at 0x0 "
                                   "must use gdbsim (with region=)"},
          "save": {"type": "boolean", "default": True,
                   "description": "keep the raw trace under .chipchamp/riscv"}},
          "required": ["elf"]})
def riscv_refrun(ctx: ToolContext, elf: str, max_steps: int = 20000,
                 march: str = "", region: str = "", timeout: float = 60,
                 model: str = "auto", harts: int = 0,
                 save: bool = True) -> dict:
    from ..riscv.toolchain import (find_iss, isa_for_elf, iss_argv,
                                   run_iss_bounded)
    from ..riscv.trace import parse_trace
    tc = _tc(ctx)
    if not tc.available:
        return {"error": "no RISC-V toolchain — run riscv.toolchain for details"}
    path = _abs(ctx, elf)
    if not os.path.isfile(path):
        return {"error": f"ELF not found: {ctx.rel(path)}"}
    backend, exe = find_iss(tc, prefer="" if model == "auto" else model)
    if model not in ("auto", "", backend):
        return {"error": f"reference model '{model}' is not installed",
                "available": backend or None}
    if not backend:
        return {"error": "no reference model available (spike, or the "
                         "toolchain's simulator)",
                "hint": "install spike (riscv-isa-sim) for a canonical model"}
    regions = [region] if region else []
    # Default the ISA from the ELF: spike assumes rv64 and refuses a 32-bit
    # payload, which would otherwise show up only as an empty trace.
    march = march or isa_for_elf(tc, path)
    argv = iss_argv(backend, exe, path, march=march, max_steps=max_steps,
                    regions=regions, harts=harts)
    try:
        # bounded: a spin-loop ending would otherwise run until killed, and a
        # plain capture would then throw away every line it printed
        # the capture bound must mirror the multi-hart instruction budget:
        # spike schedules harts in ~5000-instruction quanta, so a per-hart
        # budget's worth of lines would end inside hart 0's first quantum
        # and harts 1..N-1 would silently vanish from the trace
        _budget = harts * (max_steps + 5000) if harts > 1 else max_steps
        raw, hit_limit, timed_out = run_iss_bounded(
            argv, max_lines=max(64, _budget * 2 + 64), timeout=float(timeout))
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"reference model failed to launch: {e}"}
    steps, kind = parse_trace(raw, "spike" if backend == "spike" else "gdbsim",
                              limit=0 if harts > 1 else max_steps)
    out: dict = {"backend": backend, "steps": len(steps), "trace_kind": kind,
                 "isa": march or None,
                 "bounded": bool(hit_limit or timed_out)}
    if hit_limit or timed_out:
        out["note"] = (f"stopped at the {'step limit' if hit_limit else 'timeout'}"
                       f" — the program does not terminate (a spin loop is "
                       f"normal); the trace up to that point is used")
    if not steps:
        out["error"] = "the reference model produced no instruction trace"
        out["output"] = raw.strip().splitlines()[-10:]
        # The usual cause is a load address the model has no memory at: spike's
        # DRAM starts at 0x80000000, the gdb simulator maps low addresses. Say
        # which, instead of leaving the user with an empty trace.
        if "is invalid" in raw or "Access exception" in raw:
            out["hint"] = ("spike has no memory at this ELF's load address — its "
                           "DRAM starts at 0x80000000, so build with "
                           "base=\"0x80000000\" (riscv.compile)")
        elif "unmapped" in raw:
            out["hint"] = ("the model has no memory at the ELF's load address — "
                           "pass region=\"<base>,<size>\" (gdbsim) or rebuild at "
                           "an address it maps")
        return out
    if save:
        wd = _workdir(ctx, os.path.splitext(os.path.basename(path))[0])
        tpath = os.path.join(wd, "reference.trace")
        with open(tpath, "w") as fh:
            fh.write(raw)
        out["trace_file"] = ctx.rel(tpath)
    if backend == "gdbsim" and (march or "").startswith("rv32"):
        # Measured against spike and against real hardware (AbsoLUT rv32ui
        # add.S test_7): the gdb simulator does NOT truncate 32-bit arithmetic,
        # so 0x80000000 + 0xffff8000 compares unequal to 0x7fff8000 and the
        # test branches to its fail path. It is fine for control-flow lockstep
        # on code that does not overflow, but it is not a trustworthy RV32
        # arithmetic reference.
        out["warning"] = ("the gdb simulator mis-executes 32-bit overflow "
                          "arithmetic on RV32 — prefer spike (model=spike) "
                          "when the program's results matter")
    out["first_pc"] = f"0x{steps[0].pc:08x}"
    out["last_pc"] = f"0x{steps[-1].pc:08x}"
    out["head"] = [{"pc": f"0x{s.pc:08x}", "disasm": s.disasm}
                   for s in steps[:12]]
    return truncate(out)


@tool("riscv.cosim",
      "Co-simulate: compare a CORE's retired-instruction trace against the "
      "reference model's and report the FIRST divergence (PC, instruction, "
      "disassembly) — the difference between 'test 7 fails' and 'at PC "
      "0x80000114 the core branched but the reference did not'.",
      cost="metered", permission="submit", group="riscv",
      schema={"type": "object", "properties": {
          "elf": {"type": "string", "description": "program both sides run"},
          "core_trace": {"type": "string",
                         "description": "the core testbench's log/trace file"},
          "reference_trace": {"type": "string",
                              "description": "skip refrun and use this trace"},
          "max_steps": {"type": "integer", "default": 20000},
          "skip": {"type": "integer", "default": 0,
                   "description": "ignore N leading steps of BOTH streams"},
          "skip_ref": {"type": "integer",
                       "description": "leading REFERENCE steps to ignore; by "
                                      "default detected automatically (spike "
                                      "runs a bootrom the core never executes)"},
          "skip_dut": {"type": "integer",
                       "description": "leading CORE steps to ignore (a reset stub)"},
          "hart": {"type": "integer",
                   "description": "compare only this hardware thread. A barrel-"
                                  "multithreaded core interleaves its threads, "
                                  "so each is compared separately by default."},
          "pc_offset": {"type": "integer", "default": 0,
                        "description": "added to the CORE's PCs before "
                                       "comparing, for a core and a model that "
                                       "cannot share a memory map (a core at "
                                       "0x0 vs spike at 0x80000000)"},
          "harts": {"type": "integer", "default": 0,
                    "description": "run the reference with this many harts and "
                                   "compare hart-to-hart"},
          "march": {"type": "string",
                    "description": "ISA for the reference model; default: from "
                                   "the ELF header"},
          "model": {"type": "string", "enum": ["auto", "spike", "gdbsim"],
                    "default": "auto"},
          "region": {"type": "string"}},
          "required": ["core_trace"]})
def riscv_cosim(ctx: ToolContext, core_trace: str, elf: str = "",
                reference_trace: str = "", max_steps: int = 20000,
                skip: int = 0, skip_ref: int | None = None,
                skip_dut: int | None = None, region: str = "",
                hart: int | None = None, model: str = "auto",
                pc_offset: int = 0, harts: int = 0, march: str = "") -> dict:
    from ..riscv.trace import (align_traces, diff_traces, parse_trace,
                               split_by_hart)
    cpath = _abs(ctx, core_trace)
    if not os.path.isfile(cpath):
        return {"error": f"core trace not found: {ctx.rel(cpath)}"}
    with open(cpath, "r", errors="replace") as fh:
        # NOT limited here: a barrel core emits one line per thread per issue
        # slot, so an N-instruction budget needs N*threads lines. Capping the
        # raw parse would silently truncate the later threads (and hide a bug
        # sitting just past the cut). The budget is applied per hart below.
        dut_steps, dut_kind = parse_trace(fh.read(), "auto")
    if not dut_steps:
        return {"error": f"no retired instructions found in {ctx.rel(cpath)}",
                "hint": "the trace should carry a retired PC per line, e.g. "
                        "`pc=0x80000004 instr=0x00500093`"}
    if reference_trace:
        rpath = _abs(ctx, reference_trace)
        if not os.path.isfile(rpath):
            return {"error": f"reference trace not found: {ctx.rel(rpath)}"}
        with open(rpath, "r", errors="replace") as fh:
            ref_steps, ref_kind = parse_trace(fh.read(), "auto",
                                              limit=0 if harts > 1 else max_steps)
        backend = ref_kind
    else:
        if not elf:
            return {"error": "give elf= (to run the reference model) or "
                             "reference_trace="}
        run = riscv_refrun(ctx, elf=elf, max_steps=max_steps, region=region,
                           model=model, harts=harts, march=march)
        if "error" in run:
            return {"error": f"reference run failed: {run['error']}",
                    **{k: run[k] for k in ("hint", "backend") if k in run}}
        rpath = _abs(ctx, run["trace_file"]) if run.get("trace_file") else ""
        with open(rpath, "r", errors="replace") as fh:
            ref_steps, ref_kind = parse_trace(fh.read(), "auto",
                                              limit=0 if harts > 1 else max_steps)
        backend = run["backend"]
    if not ref_steps:
        return {"error": "the reference produced no trace"}
    # A BARREL-MULTITHREADED core interleaves its threads into one retired
    # stream; each thread runs its own program order, so they are compared
    # against the model separately. A single-threaded core is just hart 0.
    by_hart = {h: st[:max_steps] for h, st in split_by_hart(dut_steps).items()}
    if hart is not None:
        if hart not in by_hart:
            return {"error": f"the core trace has no hart {hart}",
                    "harts": sorted(by_hart)}
        by_hart = {hart: by_hart[hart]}

    ref_by_hart = split_by_hart(ref_steps)
    multi_ref = len(ref_by_hart) > 1

    def compare(stream, h=0):
        # a multi-hart reference is matched hart-to-hart; a single-hart model is
        # the golden stream for every thread (valid only when the program does
        # not branch on mhartid)
        ref_stream = ref_by_hart.get(h, ref_steps) if multi_ref else ref_steps
        # A model boots before reaching the program (spike runs a bootrom that
        # jumps to the ELF entry); the core never executes it. Align on the
        # core's first PC unless the caller pinned the offsets — dropping the
        # same count from both streams would compare nothing at all.
        _sd = skip if skip_dut is None else skip_dut
        # align on the stream AS COMPARED: anchoring on a PC that skip_dut
        # is about to drop mis-aligns the reference by exactly that skip
        # (found pairing a barrel core's warmup-trimmed trace with spike)
        _sr = skip_ref if skip_ref is not None else (
            skip if skip else align_traces(stream[_sd:], ref_stream,
                                           pc_offset=pc_offset))
        _div = diff_traces(stream, ref_stream, skip_dut=_sd, skip_ref=_sr,
                           pc_offset=pc_offset)
        return _div, _sd, _sr, min(len(stream) - _sd, len(ref_stream) - _sr)

    per_hart = {h: compare(st, h) for h, st in sorted(by_hart.items())}
    # the reported divergence is the earliest across threads
    worst = min((v for v in per_hart.values() if v[0] is not None),
                key=lambda v: v[0].index, default=None)
    div, sd, sr, _ = worst if worst else next(iter(per_hart.values()))
    depth = min(v[3] for v in per_hart.values())
    out = {"core_steps": len(dut_steps), "reference_steps": len(ref_steps),
           "core_trace_kind": dut_kind, "reference": backend,
           "skipped_reference": sr, "skipped_core": sd,
           "pc_offset": pc_offset or None,
           "reference_harts": len(ref_by_hart),
           "compared": depth, "match": div is None}
    if len(per_hart) > 1 or (per_hart and next(iter(per_hart)) != 0):
        # barrel core: say what each thread did, so a single bad thread is
        # never hidden behind an overall verdict
        out["harts"] = {h: {"steps": len(by_hart[h]), "compared": v[3],
                            "match": v[0] is None,
                            "summary": v[0].describe() if v[0] else "matched"}
                        for h, v in per_hart.items()}
        out["threads_compared"] = len(per_hart)
    # record ISA evidence for the rung-R `isa_cosim` gate (same idiom as
    # ctx.sta_delta feeding the timing gate) — a verdict the model cannot fake
    ctx.riscv_cosim = {"match": div is None, "compared": depth,
                       "reference": backend, "core_trace": ctx.rel(cpath)}
    if div is None:
        out["summary"] = f"lockstep match over {depth} retired instructions"
        if len(dut_steps) != len(ref_steps):
            # Say how far the evidence actually goes: the shorter stream ran
            # out (a bounded reference, or a testbench that stopped), so this
            # is agreement over a prefix — not proof of full equivalence.
            shorter = ("core" if len(dut_steps) - sd < len(ref_steps) - sr
                       else "reference")
            out["partial"] = True
            out["note"] = (f"agreement over a PREFIX: the {shorter} trace ended "
                           f"first ({depth} of "
                           f"{max(len(dut_steps) - sd, len(ref_steps) - sr)} "
                           f"steps). Nothing is claimed beyond that point.")
            ctx.riscv_cosim["partial"] = True
        ctx.riscv_cosim["summary"] = out["summary"]
        return truncate(out)
    out["divergence"] = {
        "index": div.index, "reason": div.reason,
        "core": {"pc": f"0x{div.dut.pc:08x}" if div.dut else None,
                 "insn": f"0x{div.dut.insn:08x}"
                         if div.dut and div.dut.insn is not None else None},
        "reference": {"pc": f"0x{div.ref.pc:08x}" if div.ref else None,
                      "disasm": div.ref.disasm if div.ref else "",
                      "insn": f"0x{div.ref.insn:08x}"
                              if div.ref and div.ref.insn is not None else None},
        "context": [{"pc": f"0x{c['pc']:08x}", "disasm": c.get("disasm", "")}
                    for c in div.context]}
    out["summary"] = div.describe()
    ctx.riscv_cosim.update(summary=out["summary"], divergence=out["divergence"])
    return truncate(out)


@tool("riscv.compliance",
      "Run the RISC-V ARCHITECTURAL TEST SUITE (riscv-arch-test) via RISCOF: "
      "executes each test on the core and on a reference model and compares "
      "signatures. This is the conformance signoff behind the `isa_compliant` "
      "gate — co-simulation shows agreement on one program, this covers the ISA.",
      cost="metered", permission="submit", group="riscv",
      schema={"type": "object", "properties": {
          "config": {"type": "string",
                     "description": "RISCOF config.ini (names the DUT + "
                                    "reference plugins). Default: [riscv] "
                                    "riscof_config, else ./config.ini"},
          "suite": {"type": "string", "description": "riscv-test-suite dir"},
          "env": {"type": "string", "description": "suite env dir"},
          "arch_test": {"type": "string",
                        "description": "riscv-arch-test checkout; suite/env are "
                                       "derived from it"},
          "testfile": {"type": "string", "description": "restrict to a testlist"},
          "timeout": {"type": "number", "default": 3600}}})
def riscv_compliance(ctx: ToolContext, config: str = "", suite: str = "",
                     env: str = "", arch_test: str = "", testfile: str = "",
                     timeout: float = 3600) -> dict:
    from ..riscv.compliance import (find_riscof, guess_suite, parse_riscof_log,
                                    riscof_argv)
    riscof = find_riscof()
    if not riscof:
        return {"error": "riscof not found on PATH",
                "hint": "install it in its own venv (it pins pyyaml==5.2): "
                        "python3 -m venv ~/.local/riscof-venv && "
                        "~/.local/riscof-venv/bin/pip install riscof && "
                        "ln -s ~/.local/riscof-venv/bin/riscof ~/.local/bin/"}
    cfg = (ctx.ws.config.get("riscv", {}) or {})
    config = config or str(cfg.get("riscof_config", "")) or "config.ini"
    config = _abs(ctx, config)
    if not os.path.isfile(config):
        return {"error": f"RISCOF config not found: {ctx.rel(config)}",
                "hint": "RISCOF needs a config.ini naming your DUT plugin and a "
                        "reference plugin — `riscof setup --dutname=<core>` "
                        "scaffolds one"}
    if not (suite and env):
        root = _abs(ctx, arch_test or str(cfg.get("arch_test", "")) or ".")
        s, e = guess_suite(root)
        suite, env = suite or s, env or e
    if not (suite and env):
        return {"error": "riscv-arch-test suite not found",
                "hint": "clone it (`riscof arch-test --clone`) and pass "
                        "arch_test=<dir>, or set [riscv] arch_test in config"}
    wd = _workdir(ctx, "compliance")
    argv = riscof_argv(riscof, suite=suite, env=env, config=config,
                       work_dir=wd, testfile=_abs(ctx, testfile) if testfile else "")
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=float(timeout), cwd=os.path.dirname(config))
    except subprocess.TimeoutExpired:
        return {"error": f"the compliance suite exceeded {timeout:.0f}s"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"riscof failed to launch: {e}"}
    log = (r.stdout or "") + "\n" + (r.stderr or "")
    logpath = os.path.join(wd, "riscof.log")
    with open(logpath, "w") as fh:
        fh.write(log)
    res = parse_riscof_log(log)
    out = {"total": res["total"], "passed": res["passed"],
           "failed": res["failed"], "failures": res["failures"][:20],
           "suite": ctx.rel(suite), "log": ctx.rel(logpath),
           "report": ctx.rel(os.path.join(wd, "report.html")),
           "ok": res["total"] > 0 and res["failed"] == 0}
    if res["total"] == 0:
        out["error"] = "riscof produced no per-test results"
        out["output"] = [ln for ln in log.strip().splitlines()[-12:]]
        return truncate(out)
    # evidence for the rung-R `isa_compliant` gate
    ctx.riscv_compliance = {"total": res["total"], "passed": res["passed"],
                            "failed": res["failed"],
                            "failures": res["failures"][:20],
                            "suite": ctx.rel(suite)}
    out["summary"] = (f"{res['passed']}/{res['total']} architectural tests passed"
                      + (f" — {res['failed']} FAILED" if res["failed"] else ""))
    return truncate(out)
