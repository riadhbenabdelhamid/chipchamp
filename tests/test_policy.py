"""Policy: classification, gates, anti-gaming, ACL, autonomy, evidence signing."""
from __future__ import annotations

from chipchamp.evidence import build_bundle
from chipchamp.policy import (Budget, ContextACL, Diff, Evidence, FileChange,
                             PolicyEngine, classify, evaluate)


def _rtl(old="always_ff x<=a;", new="always_ff x<=b;"):
    return Diff(files=[FileChange("rtl/dma/wr.sv", "modified", old, new)])


def test_classification():
    assert classify(Diff(files=[FileChange("docs/x.md", "modified")])) == "docs"
    assert classify(_rtl()) == "rtl_functional"
    d = _rtl()
    d.declared_nfc = True
    assert classify(d) == "rtl_nfc"


def test_acl_below_model():
    acl = ContextACL(deny=["secret/**"])
    assert not acl.is_allowed("pdk/tsmc/sram.lib")
    assert not acl.is_allowed("secret/key.txt")
    assert acl.is_allowed("rtl/dma.sv")


def test_protected_paths():
    eng = PolicyEngine()
    ok, _ = eng.can_write(".chipchamp/policy.yaml")
    assert not ok
    ok, _ = eng.can_write("rtl/dma.sv")
    assert ok


def test_antigaming_detects_deletion():
    eng = PolicyEngine()
    gamed = Diff(files=[FileChange("tb/t.sv", "modified",
        old_text="assert property (@(posedge clk) a |-> b);\n if (x!==y) $fatal;",
        new_text="// removed\n // removed")])
    findings = eng.check(gamed).antigaming_findings
    kinds = {f["kind"] for f in findings}
    assert "disabled_assertion" in kinds
    assert "weakened_check" in kinds


def test_report_done_blocks_without_evidence():
    eng = PolicyEngine()
    rep = eng.validate_done(_rtl(), Evidence(jobs=[]))
    assert not rep.all_passed
    assert "lint" in rep.blocking and "smoke_sim" in rep.blocking


def test_lec_inconclusive_is_not_pass():
    class J:
        def __init__(s): s.id="J";s.kind="lec";s.status="failed";s.result={"status":"inconclusive"};s.summary=""
    rep = evaluate("rtl_nfc", Evidence(jobs=[J(),
        type("S",(),{"id":"J2","kind":"lint","status":"passed","summary":"","result":{"diagnostics":[]}})(),
        type("S",(),{"id":"J3","kind":"sim","status":"passed","summary":"","result":{}})()]))
    lec = next(g for g in rep.gates if g.name == "lec")
    assert lec.status == "fail"


def test_evidence_bundle_signed_and_tamper_evident():
    rep = evaluate("docs", Evidence(jobs=[]))
    b = build_bundle("B1", "docs", change_summary={}, semantic_diff={},
                     gate_report=rep, jobs=[], narrative="x")
    assert b.verify()
    b.gate_table.append({"name": "fake", "status": "pass"})
    assert not b.verify()


def test_budget_pause():
    from chipchamp.policy import BudgetLedger
    led = BudgetLedger(Budget(v3_submissions=1))
    led.record_v3()
    ok, msg = led.can_submit_v3()
    assert not ok and "budget" in msg.lower()
