// Self-checking testbench for the counter. Uses +seed to vary the enable
// pattern so that different seeds exercise different count trajectories.
`timescale 1ns/1ps
module tb_counter;
  localparam int WIDTH = 8;
  logic clk = 0, rst_n = 0, en = 0, clr = 0;
  logic [WIDTH-1:0] count;
  logic tc;
  int errors = 0, seed = 1;
  logic [WIDTH-1:0] model = 0;

`ifdef GL  // gate-level netlist: no parameters (defaults baked at synthesis)
  counter dut (
    .clk(clk), .rst_n(rst_n), .en(en), .clr(clr), .count(count), .tc(tc));
`else
  counter #(.WIDTH(WIDTH), .WRAP(1)) dut (
    .clk(clk), .rst_n(rst_n), .en(en), .clr(clr), .count(count), .tc(tc));
`endif

  always #5 clk = ~clk;

  initial begin
`ifdef CHIPCHAMP_WAVES
    string fn;
    if ($value$plusargs("dumpfile=%s", fn)) $dumpfile(fn);
    else $dumpfile("tb_counter.vcd");
    $dumpvars(0, tb_counter);
`endif
    void'($value$plusargs("seed=%d", seed));

    rst_n = 0; repeat (3) @(negedge clk); rst_n = 1;
    model = 0;

    for (int i = 0; i < 300; i++) begin
      @(negedge clk);
      en  = (($random(seed) % 3) != 0);   // ~2/3 of cycles
      clr = (($random(seed) % 37) == 0);  // rare clear
      @(posedge clk);
      #1;
      if (clr)      model = 0;
      else if (en)  model = (model == {WIDTH{1'b1}}) ? 0 : model + 1;
      if (count !== model) begin
        errors++;
        $display("CHIPCHAMP_FAIL: counter mismatch @%0t exp=%0d got=%0d", $time, model, count);
      end
    end

    en = 0; clr = 0;
    repeat (2) @(negedge clk);
    if (errors == 0) $display("CHIPCHAMP_PASS: counter ok (300 cycles, seed=%0d)", seed);
    else             $display("CHIPCHAMP_FAIL: %0d error(s)", errors);
    $finish;
  end

  initial begin
    #200000;
    $display("CHIPCHAMP_FAIL: watchdog timeout");
    $finish;
  end
endmodule
