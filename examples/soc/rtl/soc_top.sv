// Small SoC-ish top: a counter and an arbiter in the core clock domain, and a
// sync FIFO whose data is pushed across from a second clock domain through a
// two-flop synchronizer (single-bit control only). Deliberately exercises the
// design database's hierarchy, parameter resolution and clock/reset domains.
module soc_top #(
    parameter int DATA_W = 32,
    parameter int FIFO_DEPTH = 8
) (
    input  logic              clk_core,
    input  logic              clk_io,
    input  logic              rst_n,
    input  logic              io_push,        // clk_io domain
    input  logic [DATA_W-1:0] io_data,        // clk_io domain
    input  logic              req0,
    input  logic              req1,
    output logic [DATA_W-1:0] rd_data,
    output logic              empty,
    output logic              gnt0,
    output logic              gnt1,
    output logic [7:0]        beats
);
  // --- CDC: single-bit push pulse synchronized into the core domain ---------
  logic push_meta, push_sync;
  always_ff @(posedge clk_core or negedge rst_n) begin
    if (!rst_n) begin
      push_meta <= 1'b0;
      push_sync <= 1'b0;
    end else begin
      push_meta <= io_push;   // NOTE: io_data itself is treated as quasi-static
      push_sync <= push_meta;
    end
  end

  // --- Core-domain counter counting accepted beats --------------------------
  counter #(.WIDTH(8), .WRAP(1)) u_beats (
    .clk   (clk_core),
    .rst_n (rst_n),
    .en    (push_sync),
    .clr   (1'b0),
    .count (beats),
    .tc    ()
  );

  // --- Data FIFO in the core domain -----------------------------------------
  logic full;
  logic [$clog2(FIFO_DEPTH+1)-1:0] level;
  sync_fifo #(.WIDTH(DATA_W), .DEPTH(FIFO_DEPTH)) u_fifo (
    .clk    (clk_core),
    .rst_n  (rst_n),
    .wr_en  (push_sync),
    .wr_data(io_data),
    .full   (full),
    .rd_en  (~empty),
    .rd_data(rd_data),
    .empty  (empty),
    .level  (level)
  );

  // --- Round-robin arbiter --------------------------------------------------
  arbiter_fsm u_arb (
    .clk   (clk_core),
    .rst_n (rst_n),
    .req0  (req0),
    .req1  (req1),
    .gnt0  (gnt0),
    .gnt1  (gnt1)
  );
endmodule
