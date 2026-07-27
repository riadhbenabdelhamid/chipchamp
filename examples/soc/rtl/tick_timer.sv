// Pulse generator: `pulse` goes high for one cycle every TICKS_PER_PULSE
// enabled cycles. The tick register is declared 32-bit "to be safe" — it
// provably never exceeds TICKS_PER_PULSE-1, so its upper bits are constant
// zero: the classic dead-width waste `pd.deadwidth` exists to catch, and the
// narrowing fix is provably NFC (see the optimize-loop demo test).
module tick_timer #(
    parameter int TICKS_PER_PULSE = 10
) (
    input  logic clk,
    input  logic rst_n,
    input  logic en,
    output logic pulse
);
  logic [31:0] tick_q;  // oversized: never exceeds TICKS_PER_PULSE-1
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tick_q <= '0;
      pulse  <= 1'b0;
    end else if (en) begin
      if (tick_q == TICKS_PER_PULSE - 1) begin
        tick_q <= '0;
        pulse  <= 1'b1;
      end else begin
        tick_q <= tick_q + 32'd1;
        pulse  <= 1'b0;
      end
    end else begin
      pulse <= 1'b0;
    end
  end
endmodule
