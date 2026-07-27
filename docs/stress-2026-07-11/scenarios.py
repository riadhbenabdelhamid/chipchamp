"""Stress-test scenarios for chipchamp × local models.

Each scenario: realistic FPGA/ASIC/SoC task, a setup() that mutates a fresh
workspace copy, and a validate() that computes OBJECTIVE checks (machine
truth, not model prose). Scores are 0..1 with per-check partial credit.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CHIPCHAMP = "/home/riadh/chipchamp/.venv/bin/chipchamp"


def sh(args: list[str], cwd: str | None = None, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, timeout=timeout,
                           capture_output=True, text=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, "VALIDATOR TIMEOUT"


def ac(ws: str, *args: str, timeout: int = 300) -> tuple[int, str]:
    return sh([CHIPCHAMP, "--root", ws, *args], timeout=timeout)


def _read(ws: str, rel: str) -> str:
    p = Path(ws) / rel
    return p.read_text(errors="replace") if p.exists() else ""


def newest_session(ws: str) -> dict:
    d = Path(ws) / ".chipchamp" / "sessions"
    if not d.is_dir():
        return {}
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return {}


def session_stats(ws: str) -> dict:
    """Tool-call behavior extracted from the persisted transcript."""
    sess = newest_session(ws)
    msgs = sess.get("messages", [])
    calls, errors, names = 0, 0, {}
    report_done_accepted = None
    final_holes = None
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                calls += 1
                n = tc.get("name", "?").replace("__", ".")
                names[n] = names.get(n, 0) + 1
        if m.get("role") == "tool":
            for r in m.get("content", []):
                out = r.get("output", "")
                if out.lstrip().startswith('{"error"') or '"error":' in out[:120]:
                    errors += 1
                if r.get("name") == "report.done":
                    report_done_accepted = '"accepted": true' in out
                if r.get("name") == "cov.holes":
                    m2 = re.search(r'"total_holes":\s*(\d+)', out)
                    if m2:
                        final_holes = int(m2.group(1))
    return {"tool_calls": calls, "tool_errors": errors, "tool_names": names,
            "report_done_accepted": report_done_accepted,
            "last_cov_holes": final_holes, "turns": len(msgs)}


@dataclass
class Scenario:
    name: str
    tier: str                      # core | extended | heavy
    prompt: str
    max_steps: int
    timeout_s: int
    setup: Callable[[str], None] = lambda ws: None
    validate: Callable[[str, str], dict] = lambda ws, out: {}
    notes: str = ""


# ---- setups ---------------------------------------------------------------------


def setup_triage(ws: str) -> None:
    p = Path(ws) / "rtl" / "sync_fifo.sv"
    t = p.read_text()
    assert "assign rd_data = mem[rptr];" in t, "bug anchor missing"
    p.write_text(t.replace("assign rd_data = mem[rptr];",
                           "assign rd_data = mem[wptr];"))


def setup_none(ws: str) -> None:
    pass


# ---- validators -----------------------------------------------------------------
# Each returns {"checks": {name: 0/1}, "score": 0..1, "evidence": {...}}


def _mk(checks: dict, weights: dict | None = None, **evidence) -> dict:
    w = weights or {k: 1 for k in checks}
    tot = sum(w.values()) or 1
    score = sum(w[k] for k, v in checks.items() if v) / tot
    return {"checks": {k: bool(v) for k, v in checks.items()},
            "score": round(score, 3), "evidence": evidence}


def final_answer(out: str) -> str:
    """The model's FINAL message: stdout after the last tool/plan echo line,
    before the stats footer. Prevents crediting plan text or tool echoes."""
    lines = (out or "").splitlines()
    last_echo = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(("→", "✗", "Plan (", "▶", "○", "✔", "✓")) or \
                re.match(r"^\d+\.\s", s):
            last_echo = i + 1
    tail = [ln for ln in lines[last_echo:]
            if not ln.strip().startswith("—") and "[dim]" not in ln]
    return "\n".join(tail).strip()


def val_spec_qa(ws: str, out: str) -> dict:
    ans = final_answer(out)
    text = ans.lower()
    file_line = bool(re.search(r"(sync_fifo|soc_top)\.sv:\d+", ans))
    return _mk({
        "answered": bool(text),
        "names_both_domains": ("clk_core" in text and "clk_io" in text),
        "identifies_crossing": ("push_sync" in text or "io_push" in text
                                or "2-flop" in text or "synchroniz" in text),
        "wptr_reset_located": ("sync_fifo" in text and
                               ("rst" in text or "reset" in text)),
        "cites_file_line": file_line,
    }, weights={"answered": 1, "names_both_domains": 1,
                "identifies_crossing": 1, "wptr_reset_located": 1,
                "cites_file_line": 1}, answer_chars=len(ans))


def val_triage(ws: str, out: str) -> dict:
    fixed = "assign rd_data = mem[rptr];" in _read(ws, "rtl/sync_fifo.sv")
    rc, sim = ac(ws, "--json", "sim", "fifo_smoke")
    sim_pass = '"verdict": "pass"' in sim or '"verdict":"pass"' in sim
    st = session_stats(ws)
    return _mk({
        "root_cause_named": ("wptr" in out and "rptr" in out),
        "fix_applied": fixed,
        "sim_passes_after": fixed and sim_pass,
        "report_done_accepted": bool(st.get("report_done_accepted")),
    }, weights={"root_cause_named": 1, "fix_applied": 2,
                "sim_passes_after": 2, "report_done_accepted": 1},
        sim_rc=rc)


def val_lib_reuse(ws: str, out: str) -> dict:
    st = session_stats(ws)
    lib_used = any(n.startswith("lib.") for n in st["tool_names"])
    fetched = list((Path(ws) / "rtl" / "lib").glob("*.sv*")) if (
        Path(ws) / "rtl" / "lib").is_dir() else []
    top = _read(ws, "rtl/soc_top.sv")
    integrated = any(re.search(rf"\b{f.stem}\b", top)
                     for f in fetched if f.suffix == ".sv")
    rc_l, lint = ac(ws, "--json", "lint")
    lint_errors = lint.count('"severity": "error"')
    rc_s, sim = ac(ws, "--json", "sim", "fifo_smoke")
    sim_pass = '"verdict": "pass"' in sim or '"verdict":"pass"' in sim
    return _mk({
        "lib_tools_used": lib_used,
        "component_fetched": bool(fetched),
        "instantiated_in_top": integrated,
        "lint_no_new_errors": lint_errors == 0,
        "smoke_still_passes": sim_pass,
    }, weights={"lib_tools_used": 1, "component_fetched": 2,
                "instantiated_in_top": 2, "lint_no_new_errors": 1,
                "smoke_still_passes": 2},
        fetched=[f.name for f in fetched], lint_errors=lint_errors)


def val_coverage(ws: str, out: str) -> dict:
    st = session_stats(ws)
    ran_cov = any(n in ("cov.run",) for n in st["tool_names"])
    looked_holes = "cov.holes" in st["tool_names"]
    tb_changed = "clr" in _read(ws, "tb/test_counter_cov_cocotb.py") and \
        (Path(ws) / "tb" / "test_counter_cov_cocotb.py").stat().st_mtime > \
        (Path(ws) / "rtl" / "counter.sv").stat().st_mtime
    holes_zero = st.get("last_cov_holes") == 0
    return _mk({
        "ran_coverage": ran_cov,
        "queried_holes": looked_holes,
        "tb_extended": tb_changed,
        "holes_closed": bool(holes_zero),
    }, weights={"ran_coverage": 1, "queried_holes": 1,
                "tb_extended": 2, "holes_closed": 2},
        last_holes=st.get("last_cov_holes"))


def val_rtl_edit(ws: str, out: str) -> dict:
    rtl = _read(ws, "rtl/counter.sv")
    tb = _read(ws, "tb/tb_counter.sv")
    has_port = bool(re.search(r"output\s+logic\s+overflow", rtl))
    sticky = "overflow" in rtl and ("clr" in rtl or "clear" in rtl)
    tb_covers = "overflow" in tb
    rc_l, lint = ac(ws, "--json", "lint")
    lint_errors = lint.count('"severity": "error"')
    rc_s, sim = ac(ws, "--json", "sim", "counter_smoke")
    sim_pass = '"verdict": "pass"' in sim or '"verdict":"pass"' in sim
    return _mk({
        "overflow_port_added": has_port,
        "sticky_with_clear": sticky,
        "tb_extended": tb_covers,
        "lint_clean": lint_errors == 0,
        "sim_passes": sim_pass,
    }, weights={"overflow_port_added": 2, "sticky_with_clear": 1,
                "tb_extended": 1, "lint_clean": 1, "sim_passes": 2})


def val_fpga(ws: str, out: str) -> dict:
    st = session_stats(ws)
    runs = st["tool_names"].get("fpga.run", 0)
    used_ppa = ("fpga.ppa" in st["tool_names"]
                or "fpga.checkpoints" in st["tool_names"])
    used_critical = "fpga.critical" in st["tool_names"]
    fmax_nums = [float(x) for x in re.findall(
        r"fmax[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)\s*MHz", out, re.I)]
    reported = len(fmax_nums) >= 1
    return _mk({
        "fpga_ran": runs >= 1,
        "ppa_queried": used_ppa,
        "critical_path_examined": used_critical,
        "fmax_reported": reported,
    }, weights={"fpga_ran": 2, "ppa_queried": 1,
                "critical_path_examined": 1, "fmax_reported": 1},
        fpga_runs=runs, fmax_mentions=fmax_nums[:6])


def val_skill(ws: str, out: str) -> dict:
    st = session_stats(ws)
    used = st["tool_names"].get("skill.use", 0) >= 1
    sess = json.dumps(newest_session(ws))[:400000]
    right_skill = "hw-cdc-reset" in sess
    ans = final_answer(out)
    findings = ("io_data" in ans or "push_sync" in ans or "io_push" in ans)
    file_line = bool(re.search(r"\.sv:\d+", ans))
    return _mk({
        "skill_loaded": used,
        "right_skill": right_skill,
        "real_findings": findings,
        "cites_file_line": file_line,
    }, weights={"skill_loaded": 2, "right_skill": 1,
                "real_findings": 2, "cites_file_line": 1})


def val_deadwidth(ws: str, out: str) -> dict:
    st = session_stats(ws)
    sess = json.dumps(newest_session(ws))
    measured = ("pd.deadwidth" in st["tool_names"]
                or "optimize" in json.dumps(st["tool_names"])
                or ("cov.run" in st["tool_names"]
                    and "cov.holes" in st["tool_names"]
                    and '"toggle"' in sess))  # toggle-hole route is legitimate
    lec = st["tool_names"].get("lec.run", 0) >= 1
    lec_pass = '"equivalent": true' in sess or '"verdict": "pass"' in sess
    rtl = _read(ws, "rtl/tick_timer.sv")
    narrowed = bool(re.search(r"\[\s*(9|15|7)\s*:\s*0\s*\]", rtl)
                    or re.search(r"\$clog2\s*\(", rtl))
    area_measured = "pd.area" in st["tool_names"]
    return _mk({
        "deadwidth_measured": measured,
        "narrowing_applied": narrowed,
        "lec_run": lec,
        "lec_proven": lec_pass,
        "area_quantified": area_measured,
    }, weights={"deadwidth_measured": 1, "narrowing_applied": 2,
                "lec_run": 1, "lec_proven": 2, "area_quantified": 1})


# ---- the suite -------------------------------------------------------------------


SCENARIOS: list[Scenario] = [
    Scenario(
        name="spec_qa", tier="core",
        prompt=("Answer three questions about this SoC using the design "
                "database (not guesses), citing file:line for each: "
                "(1) which clock domains exist in soc_top and which signals "
                "cross between them? (2) where is the FIFO write pointer "
                "reset? (3) what is the FIFO depth in soc_top's "
                "instantiation? Finish with a compact summary."),
        max_steps=14, timeout_s=480, setup=setup_none, validate=val_spec_qa,
        notes="tool-use quality probe: design.* tools, no writes"),
    Scenario(
        name="triage_fifo_bug", tier="core",
        prompt=("The fifo_smoke test is failing. Find the root cause with "
                "evidence (triage/waves/cone), fix the RTL, re-verify, and "
                "declare completion properly."),
        max_steps=22, timeout_s=780, setup=setup_triage, validate=val_triage,
        notes="P1 triage loop end-to-end with injected rd_data/wptr bug"),
    Scenario(
        name="lib_reuse_cdc", tier="core",
        prompt=("soc_top.sv hand-rolls a 2-flop pulse synchronizer for "
                "io_push (the always_ff on clk_core around line 24). Replace "
                "it with a verified component from the IP library instead of "
                "hand-written logic: search the library for a suitable bit/"
                "pulse synchronizer, fetch it, instantiate it in soc_top "
                "(same behavior: a 1-cycle accept pulse in the core domain), "
                "and keep lint and fifo_smoke green."),
        max_steps=22, timeout_s=780, setup=setup_none, validate=val_lib_reuse,
        notes="exercises the NEW /library feature through the agent"),
    Scenario(
        name="coverage_close", tier="core",
        prompt=("Run functional coverage for counter_cov_cocotb and load the "
                "result. There is at least one unhit bin. Identify it "
                "precisely, extend the cocotb stimulus in "
                "tb/test_counter_cov_cocotb.py to hit it (no coverage-"
                "definition edits), re-run coverage and show the hole is "
                "closed."),
        max_steps=22, timeout_s=840, setup=setup_none, validate=val_coverage,
        notes="coverage closure loop: cov.run -> cov.holes -> TB edit -> re-run"),
    Scenario(
        name="rtl_feature_edit", tier="extended",
        prompt=("Add a sticky overflow flag to rtl/counter.sv: a new output "
                "'overflow' that sets when the counter wraps and stays set "
                "until 'clr' clears it. Update tb/tb_counter.sv to exercise "
                "wrap and clear of the flag. Keep lint and counter_smoke "
                "green, then declare completion properly."),
        max_steps=22, timeout_s=780, setup=setup_none, validate=val_rtl_edit,
        notes="bread-and-butter RTL+TB feature edit with gates"),
    Scenario(
        name="fpga_timing", tier="extended",
        prompt=("Implement the counter module for an iCE40 FPGA (open flow) "
                "with a 100 MHz target. Report the achieved fmax and "
                "resource usage from the post-route checkpoint, examine the "
                "worst path in RTL terms, and state the timing margin. If "
                "fmax is below target, fix the RTL and show improvement."),
        max_steps=16, timeout_s=720, setup=setup_none, validate=val_fpga,
        notes="FPGA checkpoint flow + cross-probe (nextpnr-ice40 ~2s/run)"),
    Scenario(
        name="skill_cdc_audit", tier="extended",
        prompt=("Using the hw-cdc-reset skill as your guide, audit this SoC "
                "for clock-domain-crossing risks. Report each finding with "
                "file:line, the crossing mechanism, and whether it is safe; "
                "call out any unsynchronized crossings explicitly."),
        max_steps=14, timeout_s=600, setup=setup_none, validate=val_skill,
        notes="skills discovery + guided review (97-skill corpus loaded)"),
    Scenario(
        name="ppa_deadwidth", tier="heavy",
        prompt=("tick_timer has an oversized internal register: its tick "
                "counter is 32 bits wide but the workload never exceeds a "
                "small value. Use the PPA tooling to prove which bits are "
                "dead (toggle coverage from tick_smoke), narrow the register "
                "safely, prove logic equivalence of the change, and quantify "
                "the area recovered. Declare completion properly."),
        max_steps=26, timeout_s=1200, setup=setup_none, validate=val_deadwidth,
        notes="the optimize loop, agent-driven: deadwidth -> edit -> LEC -> area"),
]


def by_tier(tier: str) -> list[Scenario]:
    return [s for s in SCENARIOS if s.tier == tier]
