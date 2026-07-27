// Self-checking testbench for sync_fifo. Reports pass/fail via chipchamp markers
// and dumps a VCD (path from +dumpfile=) so the waveform service can query it.
`timescale 1ns/1ps
module tb_sync_fifo;
  localparam int WIDTH = 32;
  localparam int DEPTH = 8;

  logic clk = 0, rst_n = 0;
  logic wr_en = 0, rd_en = 0;
  logic [WIDTH-1:0] wr_data;
  logic [WIDTH-1:0] rd_data;
  logic full, empty;
  logic [$clog2(DEPTH+1)-1:0] level;

  int errors = 0;
  int seed = 1;

  sync_fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (
    .clk(clk), .rst_n(rst_n), .wr_en(wr_en), .wr_data(wr_data),
    .full(full), .rd_en(rd_en), .rd_data(rd_data), .empty(empty), .level(level));

  always #5 clk = ~clk;

  // reference model
  logic [WIDTH-1:0] model_q [$];

  task automatic push(input logic [WIDTH-1:0] d);
    @(negedge clk);
    if (!full) begin
      wr_en = 1; wr_data = d; model_q.push_back(d);
    end
    @(negedge clk); wr_en = 0;
  endtask

  task automatic pop();
    logic [WIDTH-1:0] exp;
    @(negedge clk);
    if (!empty) begin
      exp = model_q.pop_front();
      if (rd_data !== exp) begin
        errors++;
        $display("CHIPCHAMP_FAIL: fifo data mismatch @%0t exp=%0h got=%0h", $time, exp, rd_data);
      end
      rd_en = 1;
    end
    @(negedge clk); rd_en = 0;
  endtask

  initial begin
`ifdef CHIPCHAMP_WAVES
    string fn;
    if ($value$plusargs("dumpfile=%s", fn)) $dumpfile(fn);
    else $dumpfile("tb_sync_fifo.vcd");
    $dumpvars(0, tb_sync_fifo);
`endif
    void'($value$plusargs("seed=%d", seed));

    rst_n = 0; repeat (3) @(negedge clk); rst_n = 1;

    // fill, drain, interleave
    for (int i = 0; i < DEPTH; i++) push(32'hA000 + i);
    if (!full) begin errors++; $display("CHIPCHAMP_FAIL: expected full after %0d pushes", DEPTH); end
    for (int i = 0; i < DEPTH; i++) pop();
    if (!empty) begin errors++; $display("CHIPCHAMP_FAIL: expected empty after draining"); end

    for (int i = 0; i < 20; i++) begin
      push(32'hB000 + (i * 7));
      if (i % 2 == 0) pop();
    end
    while (!empty) pop();

    repeat (2) @(negedge clk);
    if (errors == 0) $display("CHIPCHAMP_PASS: sync_fifo ok (%0d ops)", 28);
    else             $display("CHIPCHAMP_FAIL: %0d error(s)", errors);
    $finish;
  end

  // watchdog
  initial begin
    #100000;
    $display("CHIPCHAMP_FAIL: watchdog timeout");
    $finish;
  end
endmodule
