"""evidence.bundle must not certify nothing: a narrative citing job ids the
ledger does not know is refused, and with no gate evaluated the answer is not
"all gates passed" (all-of-nothing)."""
from chipchamp.tools import all_tools


def test_bundle_refuses_job_ids_the_ledger_does_not_know(ctx):
    r = all_tools()["evidence.bundle"].handler(
        ctx, bundle_id="fake", narrative="Implementation J-9995 met timing, bitstream J-9997.")
    assert "error" in r
    assert "J-9995" in r["error"] and "J-9997" in r["error"]
    assert "job.list" in r["error"]


def test_bundle_with_no_gates_does_not_claim_they_passed(ctx):
    r = all_tools()["evidence.bundle"].handler(ctx, bundle_id="empty", narrative="nothing ran yet")
    assert "error" not in r
    assert r["gates_evaluated"] == 0
    assert r["all_gates_passed"] is None
    assert "nothing has run" in r["warning"]
    assert r["verify"] is True          # still a signed, tamper-evident record
