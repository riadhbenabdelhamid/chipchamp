"""The blind eFPGA-triage run's fix list (2026-07-31), as regression tests.

Five walls that run hit, each now load-bearing:
1. lenient tool-name resolution (`efpga__fabulous.bitstream` was a real mangle)
2. role-denied tools say so, instead of claiming the tool doesn't exist
3. the CLI labels subagent events (denials read as parent breakage otherwise)
4. every id-taking job tool teaches on a miss, like job.log already did
5. job.list exists — there was no way to FIND a job id at all
"""
from __future__ import annotations

from types import SimpleNamespace

from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse
from chipchamp.tools import all_tools


class FakeGateway:
    available = True
    model = "fake"
    ref = "fake:test"

    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, system, transcript, tools, audit_items=None):
        return self.script.pop(0)


# --- 1. lenient resolution -------------------------------------------------

def test_resolver_folds_separator_variants(ctx):
    loop = AgentLoop(ctx, FakeGateway())
    # the exact mangle observed live: separator convention on the wrong seam
    assert loop._resolve_tool_name("efpga__fabulous.bitstream") == \
        "efpga-fabulous.bitstream"
    assert loop._resolve_tool_name("sim__run") == "sim.run"      # wire form
    assert loop._resolve_tool_name(" caps ") == "caps"           # whitespace
    assert loop._resolve_tool_name("efpga‐fabulous.info") == \
        "efpga-fabulous.info"                                    # unicode dash
    assert loop._resolve_tool_name("fs.read") == "fs.read"       # exact
    # a genuine unknown passes through untouched for the error path to name
    assert loop._resolve_tool_name("totally_made_up") == "totally_made_up"


def test_resolver_used_by_dispatch(ctx):
    script = [
        ModelResponse(text="", tool_calls=[
            {"id": "t1", "name": "design__module",
             "input": {"module": "sync_fifo"}}]),
        ModelResponse(text="done", tool_calls=[]),
    ]
    events = []
    loop = AgentLoop(ctx, FakeGateway(script),
                     on_event=lambda k, d: events.append((k, d)))
    loop.run("x")
    res = [d for k, d in events if k == "tool_result"][0]["result"]
    assert res.get("card", {}).get("name") == "sync_fifo"


# --- 2. role denial vs genuinely unknown -----------------------------------

def test_role_denied_tool_says_so(ctx):
    cat = all_tools()
    subset = {n: t for n, t in cat.items() if not n.startswith("sim.")}
    loop = AgentLoop(ctx, FakeGateway(), tools=subset)
    out = loop._exec("sim.run", {"test": "x"})
    assert "not available" in out["error"]
    assert "unknown" not in out["error"]
    assert "your_tools" in out


def test_unknown_tool_suggests_neighbours(ctx):
    loop = AgentLoop(ctx, FakeGateway())
    out = loop._exec("sim.runn", {})
    assert "unknown tool" in out["error"]
    assert "sim.run" in out["error"]  # did-you-mean


# --- 3. subagent events labeled in the CLI ---------------------------------

def _fake_c(ctx):
    return SimpleNamespace(runner=SimpleNamespace(on_step=None),
                           edits={}, ws=ctx.ws,
                           gate_status=lambda: {})


def test_subagent_events_render_with_label(ctx, capsys):
    from chipchamp.cli import _agent_events
    on_event = _agent_events(_fake_c(ctx))
    on_event("tool_call", {"name": "fs.read", "input": {"path": "a.v"},
                           "subagent": "design"})
    on_event("tool_result", {"name": "caps", "subagent": "design",
                             "result": {"error": "'caps' exists but is not "
                                        "available in this agent's tool set"}})
    out = capsys.readouterr().out
    assert out.count("design ▸") == 2
    assert "fs.read" in out and "caps" in out


def test_subagent_thinking_never_spawns_streamview(ctx, capsys):
    from chipchamp.cli import _agent_events
    on_event = _agent_events(_fake_c(ctx))
    # a StreamView here would fight the parent's spinner; the labeled path
    # must swallow the whole thinking lifecycle without output or crash
    on_event("thinking_start", {"model": "m", "subagent": "verif"})
    on_event("delta", {"channel": "text", "chunk": "hi", "subagent": "verif"})
    on_event("thinking_done", {"output_tokens": 5, "subagent": "verif"})
    assert capsys.readouterr().out == ""


# --- 4. every id-taking tool teaches on a miss -----------------------------

def test_repro_and_status_teach_id_shape(ctx):
    from chipchamp.tools.meta_tools import repro
    from chipchamp.tools.verif_tools import job_status
    for fn in (repro, job_status):
        out = fn(ctx, job="fifo_smoke")
        assert "J-0249" in out["error"], fn.__name__
        assert "job.list" in out["error"], fn.__name__


# --- 5. job.list -----------------------------------------------------------

def test_job_list_registered_and_core(ctx):
    from chipchamp.agent import disclosure
    cat = all_tools()
    assert "job.list" in cat
    assert "job.list" in disclosure.core_names(cat)


def test_job_list_empty_store(ctx):
    from chipchamp.tools.verif_tools import job_list
    ctx.runner.list_jobs = lambda: []
    assert job_list(ctx) == {"jobs": []}


def test_job_list_newest_first_with_kind_filter(ctx):
    from chipchamp.tools.verif_tools import job_list
    mk = lambda i, kind: SimpleNamespace(id=f"J-{i:04d}", kind=kind,
                                         status="passed", summary=f"s{i}")
    recs = [mk(1, "lint"), mk(2, "sim"), mk(3, "sim")]
    ctx.runner.list_jobs = lambda: recs
    out = job_list(ctx, limit=2)
    assert [j["job"] for j in out["jobs"]] == ["J-0003", "J-0002"]
    out = job_list(ctx, kind="lint")
    assert [j["job"] for j in out["jobs"]] == ["J-0001"]


# --- 6. eFPGA jobs declare honest inputs -----------------------------------

def test_efpga_inputs_cover_fabric_state(tmp_path):
    from chipchamp.tools.efpga_tools import _efpga_inputs
    from chipchamp.util.hashing import hash_manifest
    proj = tmp_path / "proj"
    (proj / "Tile" / "LUT4AB").mkdir(parents=True)
    (proj / ".FABulous").mkdir()
    (proj / "user_design").mkdir()
    (proj / "fabric.csv").write_text("FabricBegin\n")
    (proj / "Tile" / "LUT4AB" / "m.list").write_text("N1BEG[0|0],[a|b]\n")
    (proj / ".FABulous" / "pips.txt").write_text("edge1\nedge2\n")
    (proj / "user_design" / "d.v").write_text("module d; endmodule\n")

    fab = _efpga_inputs(str(proj))
    assert any(p.endswith("fabric.csv") for p in fab)
    assert any(p.endswith("m.list") for p in fab)
    assert not any("user_design" in p for p in fab)  # fabric gen: no design

    bit = _efpga_inputs(str(proj), "user_design/d.v")
    assert any(p.endswith("pips.txt") for p in bit)
    assert any(p.endswith("d.v") for p in bit)

    # THE property: two jobs straddling a fabric corruption must not share a
    # hash — the empty-manifest constant made a live agent call a
    # deterministic fault "nondeterministic nextpnr"
    before = hash_manifest(_efpga_inputs(str(proj), "user_design/d.v"))
    (proj / "Tile" / "LUT4AB" / "m.list").write_text("N1BEG0,a\n")
    after = hash_manifest(_efpga_inputs(str(proj), "user_design/d.v"))
    assert before != after


# --- 7. fs defaults must see the whole vertical ----------------------------

def test_fs_defaults_see_non_sv_sources(tmp_path):
    # own tmp workspace: the shared `ctx` fixture points at the REAL
    # examples/soc, which a test must never mutate
    import os
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    from chipchamp.tools.fs_tools import fs_grep, fs_list, fs_read
    ctx = ToolContext(Workspace(str(tmp_path)))
    proj = os.path.join(ctx.ws.root, "fab")
    os.makedirs(os.path.join(proj, "user_design"))
    with open(os.path.join(proj, "user_design", "lfsr.v"), "w") as fh:
        fh.write("module lfsr; endmodule\n")
    with open(os.path.join(proj, "matrix.list"), "w") as fh:
        fh.write("N1BEG[0|0],[a|b]\n")

    # default pattern: .v and .list are source files too — the *.sv-only
    # default made a live agent conclude its target project did not exist
    files = fs_list(ctx, dir="fab")["files"]
    assert "fab/user_design/lfsr.v" in files
    assert "fab/matrix.list" in files

    hits = fs_grep(ctx, pattern="module lfsr")["hits"]
    assert any("lfsr.v" in h for h in hits)

    # an empty match on a non-empty dir says WHY it is empty
    out = fs_list(ctx, dir="fab", pattern="*.xyz")
    assert out["files"] == [] and "none matched" in out["note"]
    assert "error" in fs_list(ctx, dir="no_such_dir")

    # a missing path with the right basename elsewhere teaches the real one
    out = fs_read(ctx, path="user_design/lfsr.v")
    assert "did you mean: fab/user_design/lfsr.v" in out["error"]


# --- 8. id allocation survives a vanished store dir ------------------------

def test_next_id_recreates_missing_store_dir(tmp_path):
    import shutil
    from chipchamp.jobs.runner import JobRunner
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    first = r._next_id()
    shutil.rmtree(tmp_path / "runs")
    second = r._next_id()  # must recreate the dir, not crash the submit
    assert second != first
    assert (tmp_path / "runs" / "seq.txt").read_text() == "2"


# --- 9. eFPGA bitstream tool teaches on wrong design arguments -------------

def test_bitstream_rejects_wrapper_and_bare_names(ctx):
    from chipchamp.tools.efpga_tools import efpga_bitstream
    out = efpga_bitstream(ctx, design="user_design/top_wrapper.v")
    assert "WRAPPER" in out["error"] and "user_design/" in out["error"]
    out = efpga_bitstream(ctx, design="top_wrapper")
    assert "WRAPPER" in out["error"]
    out = efpga_bitstream(ctx, design="lfsr_bank")
    assert "not a design file path" in out["error"]
    assert "user_design/lfsr_bank.v" in out["error"]


def test_bitstream_failure_summary_names_the_reason():
    from chipchamp.adapters.fabulous import _fail_reason, _summary
    log = ("yosys output...\n"
           "ERROR: Unable to place cell '$abc$123', no BELs remaining to "
           "implement cell type 'FABULOUS_LC'\nmore lines\n")
    assert "no BELs remaining" in _fail_reason(log)
    m = {"bitstream_bytes": 0, "routed": None,
         "fail_reason": _fail_reason(log)}
    assert "no BELs remaining" in _summary("bitstream", m, ok=False)
    # redefinition (the wrapper-as-design crash) outranks the generic tail
    log2 = "a\ntop_wrapper.v:4: ERROR: Re-definition of module `$abstract\\top_wrapper'!\nb\n"
    assert "Re-definition" in _fail_reason(log2)


# --- 10. shrinking rewrites warn -------------------------------------------

def test_fs_write_shrink_warns(tmp_path):
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    from chipchamp.tools.fs_tools import fs_write
    ctx = ToolContext(Workspace(str(tmp_path)))
    big = "\n".join(f"row{i}" for i in range(40)) + "\n"
    fs_write(ctx, path="floor.csv", content=big)
    out = fs_write(ctx, path="floor.csv", content="row0\nrow1\n")
    assert "fs.edit" in out.get("note", "")          # a live agent lost
    out = fs_write(ctx, path="floor.csv", content=big)   # growth: no nag
    assert "note" not in out


# --- 11. one workspace, one id authority -----------------------------------

def test_workspace_runner_is_memoized(tmp_path):
    from chipchamp.config import Workspace
    ws = Workspace(str(tmp_path))
    assert ws.runner() is ws.runner()


# --- 12. a registered dir may BE a skill -----------------------------------

def test_skill_dir_with_root_manifest(tmp_path, monkeypatch):
    # `chipchamp skills add ~/wavelet` where the repo root carries SKILL.md:
    # a live user did this and got "0 skill(s) found"
    from chipchamp.config import Workspace
    from chipchamp import skills as sk
    root = tmp_path / "toolrepo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: tool-driver\ndescription: drives the tool\n---\nbody\n")
    ws = Workspace(str(tmp_path / "ws"))
    monkeypatch.setattr(sk, "skill_dirs", lambda ws: [(root, "registry")])
    rep = sk.discover_report(ws) if hasattr(sk, "discover_report") else None
    found = sk.discover(ws)
    assert "tool-driver" in found


# --- 13. streamed markdown commits only completed blocks -------------------

def test_streamview_stable_boundary():
    from chipchamp.ui import StreamView
    # a growing table has no blank lines after the intro — nothing beyond
    # the intro may be committed, or re-padded rows duplicate in scrollback
    lines = ["intro paragraph", "", "| a | b |", "| 1 | 2 |", "| 3 | 4 |"]
    assert StreamView._stable_boundary(lines) == 1
    # ANSI-styled blank lines still count as boundaries
    lines = ["para", "\x1b[2m\x1b[0m", "| t |"]
    assert StreamView._stable_boundary(lines) == 1
    assert StreamView._stable_boundary(["| only | table |"]) == 0


# --- 14. bitstream-vs-RTL cosim tool ---------------------------------------

def test_simulate_tool_teaches_and_generates_tb(tmp_path, ctx):
    import os
    from chipchamp.tools.efpga_tools import _ensure_tb, efpga_simulate
    # missing bitstream: teach the order of operations, name the exact path
    out = efpga_simulate(ctx, design="user_design/ghost.v",
                         project=str(tmp_path))
    assert "no bitstream at user_design/ghost.bin" in out["error"]
    assert "efpga-fabulous.bitstream" in out["error"]
    # TB generation is name substitution from the template
    os.makedirs(tmp_path / "Test")
    (tmp_path / "Test" / "sequential_16bit_en_tb.v").write_text(
        "module sequential_16bit_en_tb;\n"
        "sequential_16bit_en dut_i ();\nendmodule\n")
    tb = _ensure_tb(str(tmp_path), "mydsp")
    body = open(tb).read()
    assert "mydsp dut_i" in body and "sequential_16bit_en" not in body
    # an existing TB is never overwritten
    (tmp_path / "Test" / "custom_tb.v").write_text("handwritten\n")
    assert open(_ensure_tb(str(tmp_path), "custom")).read() == "handwritten\n"


def test_simulate_adapter_plan_and_summary():
    from chipchamp.adapters.fabulous import FabulousAdapter, _summary
    plan = FabulousAdapter().simulate("/p", fmt="vcd",
                                      bitstream="user_design/d.bin")
    assert "run_simulation vcd user_design/d.bin" in " ".join(
        plan.steps[0].argv)
    assert plan.meta["step"] == "simulate"
    assert plan.artifacts["waveform"].endswith("Test/build/d.vcd")
    assert "PASSED" in _summary("simulate", {"sim_passed": True}, ok=True)
    assert "FAILED" in _summary("simulate", {"fail_reason": "x"}, ok=False)


# --- 15. a cosim job must not shadow the E-gate evidence -------------------

def test_efpga_gates_survive_a_later_simulate_job():
    from chipchamp.policy.gates import _efpga_metrics

    class J(SimpleNamespace):
        pass

    def job(ts, **metrics):
        return J(start_ts=ts, result={"metrics": metrics},
                 kind="efpga-fabulous")

    class Ev:
        def __init__(self, jobs): self._j = jobs
        def jobs_of(self, kind): return self._j

    ev = Ev([job(1, step="fabric", fabric_generated=True, fabric_files=55),
             job(2, step="bitstream", bitstream_bytes=12024, routed=True),
             job(3, step="simulate", sim_passed=True)])
    m = _efpga_metrics(ev)
    # the simulate job is newest — the gate must still see the bitstream
    assert m.get("bitstream_bytes") == 12024


# --- 16. a diagnosis is a claim: root_cause closes must cite evidence ------

def _ev_with_notes(jobs, notes):
    from chipchamp.policy.gates import Evidence
    return Evidence(jobs=jobs, root_cause_notes=notes)


def _job(jid, status):
    return SimpleNamespace(id=jid, kind="efpga-fabulous", status=status,
                           result={"metrics": {}})


def test_root_cause_close_with_zero_jobs_is_rejected():
    from chipchamp.policy.gates import evaluate
    ev = _ev_with_notes([], [{"id": "N-1", "kind": "root_cause",
                              "subject": "the fabric broke",
                              "detail": "trust me", "evidence": ""}])
    rep = evaluate("investigation", ev)
    g = next(x for x in rep.gates if x.name == "diagnosis_cited")
    assert g.status == "fail" and "NO jobs" in g.detail


def test_root_cause_citing_failing_job_passes():
    from chipchamp.policy.gates import evaluate
    ev = _ev_with_notes(
        [_job("J-0425", "failed"), _job("J-0427", "passed")],
        [{"id": "N-2", "kind": "root_cause", "subject": "routing starvation",
          "detail": "muxes collapsed", "evidence": "J-0425"}])
    g = next(x for x in evaluate("investigation", ev).gates
             if x.name == "diagnosis_cited")
    assert g.status == "pass"


def test_root_cause_citing_only_passing_when_failures_exist_fails():
    from chipchamp.policy.gates import evaluate
    ev = _ev_with_notes(
        [_job("J-0425", "failed"), _job("J-0427", "passed")],
        [{"id": "N-3", "kind": "root_cause", "subject": "s",
          "detail": "see J-0427", "evidence": ""}])
    g = next(x for x in evaluate("investigation", ev).gates
             if x.name == "diagnosis_cited")
    assert g.status == "fail" and "J-0425" in g.detail


def test_no_root_cause_notes_no_gate():
    from chipchamp.policy.gates import evaluate
    ev = _ev_with_notes([_job("J-1", "passed")], [])
    assert not any(x.name == "diagnosis_cited"
                   for x in evaluate("investigation", ev).gates)


# --- 17. cosim alignment respects skip_dut ---------------------------------

def test_cosim_aligns_on_the_skipped_stream(ctx):
    from chipchamp.tools.riscv_tools import riscv_cosim
    import os
    # core trace with ONE bogus warmup line; reference starts at the real pc
    core = os.path.join(ctx.ws.root, "core_t.log")
    ref = os.path.join(ctx.ws.root, "ref_t.log")
    with open(core, "w") as fh:
        fh.write("pc=0xdead0000 instr=0x00000013\n"        # warmup garbage
                 "pc=0x00000000 instr=0x00500093\n"
                 "pc=0x00000004 instr=0x00300113\n")
    with open(ref, "w") as fh:
        fh.write("[TRACE] pc=0x00000000 instr=0x00500093\n"
                 "[TRACE] pc=0x00000004 instr=0x00300113\n")
    out = riscv_cosim(ctx, core_trace="core_t.log",
                      reference_trace="ref_t.log", skip_dut=1)
    # before the fix, auto-align anchored on the garbage pc the skip removed
    assert out.get("match") is True, out.get("summary")
    os.unlink(core); os.unlink(ref)


# --- 18. writeback-level cosim: parser + multi-hart reference budget -------

def test_rtl_trace_carries_writebacks():
    from chipchamp.riscv.trace import parse_rtl_trace
    st = parse_rtl_trace(
        "hart=3 pc=0x00000040 instr=0x40365693 rd=13 data=0xfffffff8\n")[0]
    assert (st.hart, st.reg, st.value) == (3, "x13", 0xfffffff8)
    # lines without a writeback stay comparison-neutral
    st2 = parse_rtl_trace("pc=0x4 instr=0x13\n")[0]
    assert st2.reg == "" and st2.value is None


def test_spike_multihart_budget_covers_every_quantum():
    from chipchamp.riscv.toolchain import iss_argv
    argv = iss_argv("spike", "spike", "p.elf", march="rv32i_zicsr",
                    max_steps=400, regions=[], harts=10)
    # spike schedules harts in ~5000-instruction quanta: a per-hart budget
    # must scale, or harts 1..N-1 never execute (found live: a 10-hart
    # reference trace containing only core 0)
    assert "-p10" in argv
    assert f"--instructions={10 * (400 + 5000)}" in argv
    solo = iss_argv("spike", "spike", "p.elf", march="", max_steps=400,
                    regions=[], harts=0)
    assert "--instructions=400" in solo and not any(
        a.startswith("-p") for a in solo if a != "p.elf")


# --- 19. memory-op cosim: parse, diff, offset equality ---------------------

def test_mem_ops_parse_and_diff():
    from chipchamp.riscv.trace import (TraceStep, diff_traces,
                                       parse_rtl_trace, parse_spike_trace)
    # rtl store line
    st = parse_rtl_trace("hart=2 pc=0x1c instr=0x001a2023 "
                         "mem=0x00000060 wdata=0x00000005\n")[0]
    assert (st.mem_addr, st.mem_wdata) == (0x60, 0x5)
    # spike store and load commit lines
    sp = parse_spike_trace(
        "core   1: 3 0x8000001c (0x001a2023) mem 0x80000060 0x00000005\n")[0]
    assert (sp.mem_addr, sp.mem_wdata) == (0x80000060, 0x5)
    ld = parse_spike_trace(
        "core   0: 3 0x80000030 (0x00052503) x10 0x2a mem 0x80000420\n")[0]
    assert (ld.reg, ld.value, ld.mem_addr, ld.mem_wdata) == \
        ("x10", 0x2a, 0x80000420, None)

    def mk(pc, ma=None, mw=None, reg="", val=None):
        return TraceStep(pc=pc, insn=0x23, mem_addr=ma, mem_wdata=mw,
                         reg=reg, value=val)

    OFF = 0x80000000
    # matching store through the image offset
    assert diff_traces([mk(0x1c, 0x60, 5)], [mk(OFF + 0x1c, OFF + 0x60, 5)],
                       pc_offset=OFF) is None
    # wrong store address → mem divergence
    d = diff_traces([mk(0x1c, 0x70, 5)], [mk(OFF + 0x1c, OFF + 0x60, 5)],
                    pc_offset=OFF)
    assert d and d.reason == "mem" and "0x80000070" in d.describe()
    # wrong store data → mem divergence
    d = diff_traces([mk(0x1c, 0x60, 6)], [mk(OFF + 0x1c, OFF + 0x60, 5)],
                    pc_offset=OFF)
    assert d and d.reason == "mem"
    # an ADDRESS-valued writeback differs by exactly the offset: equal
    assert diff_traces([mk(0x4, reg="x20", val=0x60)],
                       [mk(OFF + 0x4, reg="x20", val=OFF + 0x60)],
                       pc_offset=OFF) is None


# --- 20. the footer leads with the model -----------------------------------

def test_footer_model_segment_leads_and_is_valid_markup():
    from types import SimpleNamespace as NS
    from prompt_toolkit.formatted_text import HTML
    from chipchamp.cli import _board_leds
    _board_leds._cache = None
    c = NS(runner=NS(list_jobs=lambda: [NS(kind="sim", status="passed")]))
    loop = NS(gateway=NS(ref="ollama:qwen3.6:35b", model="qwen3.6:35b",
                         reasoning_effort="high"))
    out = _board_leds(c, loop)
    HTML(out)                                   # malformed markup raises
    assert out.index("qwen3.6:35b") < out.index("sim")   # model first
    assert "ollama" in out and "↯high" in out


# --- 21. UI polish: theme lever, ladder states, readable chronogram --------

def test_theme_retints_standard_styles():
    from rich.console import Console
    from chipchamp import ui
    assert set(ui.theme_names()) >= {"phosphor", "amber", "scope", "mono"}
    for name, green in (("phosphor", "#a8ff00"), ("amber", "#ffb000"),
                        ("scope", "#39d353"), ("mono", "#e8e8e8")):
        con = Console()
        con.push_theme(ui._THEMES[name])
        assert str(con.get_style("green").color.triplet.hex).lower() == green, name


def test_gate_ladder_states_and_verbatim_names():
    from chipchamp.ui import gate_ladder
    rep = SimpleNamespace(min_rung="E", task_class="efpga-fabulous", gates=[
        SimpleNamespace(name="fabric_generated", status="pass", ok=True),
        SimpleNamespace(name="bitstream_generated", status="fail", ok=False),
        SimpleNamespace(name="diagnosis_cited", status="missing", ok=False)])
    out = gate_ladder(rep)
    # names verbatim (they are report.done's rejection vocabulary)
    for n in ("fabric_generated", "bitstream_generated", "diagnosis_cited"):
        assert n in out
    assert "✗" in out and "○" in out and "[red]" in out and "1/3" in out


def test_chronogram_axis_is_readable():
    from chipchamp.waves.render import ascii_timing, fmt_time
    assert fmt_time(20664500, "1ps") == "20.66µs"
    assert fmt_time(42, "") == "42"
    snap = {"window": [1000, 9000], "timescale": "1ns", "signals": {
        "tb.clk": {"width": 1, "value_at_start": "0",
                   "edges": [(1000 + i * 500, str(i % 2)) for i in range(16)]},
        "tb.d": {"width": 8, "value_at_start": "0",
                 "edges": [(1000 + i * 1000, bin(i)[2:]) for i in range(8)]}}}
    out = ascii_timing(snap, width=80)
    lines = out.splitlines()
    assert lines[0].startswith("t 1µs … 9µs")      # real units, not run-ons
    assert "tb.*" in lines[0]                       # common prefix in title
    assert lines[1].startswith("clk")               # trimmed names
    assert "╵+0" in lines[-1]                       # tick ruler
    assert all(len(l) <= 80 for l in lines)         # width respected


# --- 22. /theme: live switch + persistence ---------------------------------

def test_set_theme_switches_and_reverts():
    from chipchamp import ui
    assert ui.set_theme("phosphor") == "phosphor"
    assert ui.set_theme("nonsense") == "phosphor"     # unknown: no-op
    assert ui.set_theme("off") == ""                  # back to defaults


def test_persist_theme_is_surgical(tmp_path):
    from types import SimpleNamespace as NS
    from chipchamp.cli import _persist_theme
    cfg = tmp_path / "config.toml"
    cfg.write_text("[model]\nref = 'x'\n\n[ui]\n# comment kept\n")
    c = NS(ws=NS(dot=tmp_path))
    _persist_theme(c, "phosphor")
    t = cfg.read_text()
    assert 'theme = "phosphor"' in t and "[model]" in t and "# comment" in t
    _persist_theme(c, "")                             # off → cleared, once
    t = cfg.read_text()
    assert t.count("theme =") == 1 and 'theme = ""' in t
