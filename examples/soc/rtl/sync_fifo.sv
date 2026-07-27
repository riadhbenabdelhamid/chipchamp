// Synchronous (single-clock) FIFO with first-word-fall-through read.
// Depth need not be a power of two; pointers are compared, not masked.
module sync_fifo #(
    parameter int WIDTH = 32,
    parameter int DEPTH = 8
) (
    input  logic              clk,
    input  logic              rst_n,
    input  logic              wr_en,
    input  logic [WIDTH-1:0]  wr_data,
    output logic              full,
    input  logic              rd_en,
    output logic [WIDTH-1:0]  rd_data,
    output logic              empty,
    output logic [$clog2(DEPTH+1)-1:0] level
);
  localparam int AW = $clog2(DEPTH);

  logic [WIDTH-1:0] mem [DEPTH];
  logic [AW-1:0]    wptr, rptr;
  logic [AW:0]      count;

  assign full  = (count == DEPTH[AW:0]);
  assign empty = (count == '0);
  assign level = count;

  wire do_wr = wr_en & ~full;
  wire do_rd = rd_en & ~empty;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wptr  <= '0;
      rptr  <= '0;
      count <= '0;
    end else begin
      if (do_wr) begin
        mem[wptr] <= wr_data;
        wptr <= (wptr == AW'(DEPTH-1)) ? '0 : wptr + 1'b1;
      end
      if (do_rd)
        rptr <= (rptr == AW'(DEPTH-1)) ? '0 : rptr + 1'b1;
      count <= count + (do_wr ? 1 : 0) - (do_rd ? 1 : 0);
    end
  end

  assign rd_data = mem[rptr];
endmodule
