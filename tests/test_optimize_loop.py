"""THE optimization loop, end to end, live: coverage flags an oversized
register → deadwidth prices it → the fix is applied → **eqy (PDR strategy)
proves the narrowing is NFC** → area delta measures the win → the full
rtl_nfc gate set (lint, smoke, LEC, no-gaming) accepts report.done.

This is the exact sequence an agent session runs on an `chipchamp optimize`
worklist item; here it's scripted so the loop itself is regression-tested.
The workspace edit is restored afterward."""
from __future__ import annotations

import os
import shutil

import pytest

from chipchamp.physical.area import find_liberty
from chipchamp.tools import all_tools

requires_loop_stack = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("yosys", "verilator", "iverilog", "eqy"))
    or find_liberty() is None,
    reason="needs yosys+verilator+iverilog+eqy and a sky130 liberty")

NARROWED = """\
// Pulse generator: `pulse` goes high for one cycle every TICKS_PER_PULSE
// enabled cycles. tick_q is sized to what it actually counts
// ($clog2(TICKS_PER_PULSE) bits) — narrowed from a 32-bit declaration after
// pd.deadwidth flagged bits [4:31] as never toggling; LEC-proven NFC.
module tick_timer #(
    parameter int TICKS_PER_PULSE = 10
) (
    input  logic clk,
    input  logic rst_n,
    input  logic en,
    output logic pulse
);
  localparam int TW = $clog2(TICKS_PER_PULSE);
  logic [TW-1:0] tick_q;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tick_q <= '0;
      pulse  <= 1'b0;
    end else if (en) begin
      if (tick_q == TW'(TICKS_PER_PULSE - 1)) begin
        tick_q <= '0;
        pulse  <= 1'b1;
      end else begin
        tick_q <= tick_q + TW'(1);
        pulse  <= 1'b0;
      end
    end else begin
      pulse <= 1'b0;
    end
  end
endmodule
"""


@requires_loop_stack
def test_deadwidth_optimize_loop_end_to_end(ctx, tmp_path):
    t = all_tools()
    rtl = os.path.join(ctx.ws.root, "rtl", "tick_timer.sv")
    golden_text = open(rtl).read()
    golden_copy = tmp_path / "tick_timer_golden.sv"
    golden_copy.write_text(golden_text)
    try:
        # 1. FIND: workload coverage flags the oversized register
        cov = t["cov.run"].handler(ctx, test="tick_smoke", set="loop")
        assert "error" not in cov, cov
        base = t["pd.area"].handler(ctx, top="tick_timer")
        assert "error" not in base, base
        base_area = base["total_area_um2"]
        dw = t["pd.deadwidth"].handler(ctx, top="tick_timer", set="loop")
        assert "error" not in dw, dw
        f = next(x for x in dw["findings"] if x["signal"] == "tick_q")
        assert f["width"] == 32 and f["dead"] == 28  # [4:31] never toggle
        assert f["wasted_area_um2"] > 400            # ~28 flops priced
        assert "never toggled" in f["hint"]

        # 2. FIX: the edit an agent applies from the worklist item
        with open(rtl, "w") as fh:
            fh.write(NARROWED)
        ctx.record_edit("rtl/tick_timer.sv", golden_text, NARROWED, "modified")

        # 3. PROVE: narrowing re-encodes state — k-induction can't close it,
        # the adapter's PDR strategy discovers the reachability invariant
        lec = t["lec.run"].handler(ctx, module="tick_timer",
                                   golden_files=[str(golden_copy)],
                                   revised_files=[rtl])
        assert lec["verdict"] == "equivalent", lec

        # 4. MEASURE: the win, in µm²
        opt = t["pd.area"].handler(ctx, top="tick_timer")
        saved = round(base_area - opt["total_area_um2"], 2)
        assert saved > 400, (base_area, opt["total_area_um2"])
        # most of the priced waste is actually recovered
        assert saved >= 0.7 * f["wasted_area_um2"]

        # 5. GATES: the change is NFC-classified and report.done ACCEPTS it
        # on real job records (lint + smoke sim + LEC + no-gaming)
        chk = t["policy.check"].handler(ctx, declare_nfc=True)
        assert chk["task_class"] == "rtl_nfc", chk
        lint = t["lint.run"].handler(ctx, paths=["rtl/tick_timer.sv"])
        assert "error" not in lint
        sim = t["sim.run"].handler(ctx, test="tick_smoke", seed=1)
        assert sim["sim_status"] == "pass", sim
        done = t["report.done"].handler(
            ctx, summary="narrowed tick_q per deadwidth finding (LEC-proven)")
        assert done["accepted"], done["blocking_gates"]
    finally:
        with open(rtl, "w") as fh:
            fh.write(golden_text)
