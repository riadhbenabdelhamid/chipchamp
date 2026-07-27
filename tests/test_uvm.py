"""UVM report parsing (simulator-neutral) + the verdict rule: report-server
counts override exit codes. Live Verilator+uvm-core run in test_uvm_live.py."""
from __future__ import annotations

from chipchamp.adapters.base import StepResult, ToolResult
from chipchamp.adapters.icarus import _status_from_log
from chipchamp.adapters.uvm import enrich_sim_result, parse_uvm_log

# a passing run, Verilator/VCS style (no transcript prefix)
PASS_LOG = """\
UVM_INFO @ 0: reporter [RNTST] Running test smoke_test...
UVM_INFO verif/smoke_test.sv(18) @ 0: uvm_test_top [SMOKE] hello
UVM_INFO verif/smoke_test.sv(21) @ 10: uvm_test_top [SMOKE] objection dropping
UVM_INFO @ 10: reporter [UVM/REPORT/SERVER]
--- UVM Report Summary ---

** Report counts by severity
UVM_INFO :    4
UVM_WARNING :    1
UVM_ERROR :    0
UVM_FATAL :    0
** Report counts by id
[RNTST]     1
[SMOKE]     2
$finish called at 10 (1ns)
"""

# a failing run, Questa transcript style ("# " prefix), exit code 0
FAIL_LOG = """\
# UVM_INFO @ 0: reporter [RNTST] Running test axi_burst_test...
# Sverilog seed = 7
# UVM_ERROR verif/axi_scoreboard.sv(88) @ 105000: uvm_test_top.env.sb [SB_MISMATCH] exp 0xd3 got 0x7f
# UVM_ERROR verif/axi_scoreboard.sv(91) @ 106000: uvm_test_top.env.sb [SB_MISMATCH] exp 0x00 got 0x80
# UVM_INFO @ 200000: reporter [UVM/REPORT/SERVER]
# --- UVM Report Summary ---
#
# ** Report counts by severity
# UVM_INFO :   12
# UVM_WARNING :    0
# UVM_ERROR :    2
# UVM_FATAL :    0
# ** Report counts by id
# [SB_MISMATCH]     2
"""

# a hung run: killed before the report server printed a summary
DIED_LOG = """\
UVM_INFO @ 0: reporter [RNTST] Running test dma_soak_test...
UVM_INFO verif/dma_env.sv(40) @ 1000: uvm_test_top.env [ENV] started
"""


def _res(status="pass"):
    return ToolResult(ok=(status == "pass"), kind="sim", adapter="x",
                      status=status, summary="completed")


def test_parse_passing_run():
    r = parse_uvm_log(PASS_LOG)
    assert r.is_uvm and r.summary_seen
    assert r.test == "smoke_test"
    assert r.counts == {"INFO": 4, "WARNING": 1, "ERROR": 0, "FATAL": 0}
    assert r.verdict() == "pass" and not r.diagnostics


def test_parse_failing_run_questa_prefix():
    r = parse_uvm_log(FAIL_LOG)
    assert r.is_uvm and r.errors == 2 and r.fatals == 0
    assert r.test == "axi_burst_test"
    assert r.seed == 7
    assert r.verdict() == "fail"
    assert len(r.diagnostics) == 2
    d = r.diagnostics[0]
    assert d.file == "verif/axi_scoreboard.sv" and d.line == 88
    assert d.code == "UVM_ERROR:SB_MISMATCH" and "exp 0xd3" in d.message


def test_no_summary_means_fail():
    r = parse_uvm_log(DIED_LOG)
    assert r.is_uvm and not r.summary_seen
    assert r.verdict() == "fail"


def test_plain_tb_log_passes_through():
    r = parse_uvm_log("CHIPCHAMP_PASS\nall good\n")
    assert not r.is_uvm
    res = enrich_sim_result(_res("pass"), "CHIPCHAMP_PASS\n")
    assert res.status == "pass" and "uvm" not in res.metrics


def test_enrich_downgrades_exit0_uvm_errors():
    """The non-negotiable: UVM_ERROR>0 fails the job even on a clean exit."""
    res = enrich_sim_result(_res("pass"), FAIL_LOG)
    assert res.status == "fail" and not res.ok
    assert res.metrics["uvm_errors"] == 2 and res.metrics["uvm_test"] == "axi_burst_test"
    assert res.metrics["seed"] == 7  # recovered from the transcript
    assert "SB_MISMATCH" in res.diagnostics[-1].code


def test_enrich_never_upgrades_a_failure():
    res = enrich_sim_result(_res("fail"), PASS_LOG)
    assert res.status == "fail"


def test_status_from_log_ignores_summary_tallies():
    """'UVM_FATAL :    0' is a tally, not a fatal — a passing UVM log must not
    trip the keyword checks."""
    sr = StepResult(argv=["sim"], rc=0, stdout=PASS_LOG, stderr="")
    status, _ = _status_from_log(PASS_LOG, sr)
    assert status == "pass"


def test_enriched_pass_summary_names_test():
    res = enrich_sim_result(_res("pass"), PASS_LOG)
    assert res.status == "pass" and "smoke_test" in res.summary


def test_parse_real_verilator_uvm_log():
    """REAL output of Accellera uvm-core 2020.3.1 under Verilator 5.049
    (captured live on this machine)."""
    import os
    fix = os.path.join(os.path.dirname(__file__), "fixtures",
                       "uvm_verilator_smoke.log")
    r = parse_uvm_log(open(fix).read())
    assert r.is_uvm and r.summary_seen
    assert r.counts == {"INFO": 6, "WARNING": 2, "ERROR": 0, "FATAL": 0}
    assert r.verdict() == "pass"
    res = enrich_sim_result(_res("pass"), open(fix).read())
    assert res.ok and res.metrics["uvm_warnings"] == 2
