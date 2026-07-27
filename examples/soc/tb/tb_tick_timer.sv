// Self-checking testbench for tick_timer: with en held high, pulse must be
// high exactly every TICKS_PER_PULSE-th cycle and low otherwise.
`timescale 1ns/1ps
module tb_tick_timer;
  localparam int TPP = 10;
  logic clk = 0, rst_n = 0, en = 0, pulse;
  int errors = 0;

  tick_timer #(.TICKS_PER_PULSE(TPP)) dut (
    .clk(clk), .rst_n(rst_n), .en(en), .pulse(pulse));

  always #5 clk = ~clk;

  initial begin
`ifdef CHIPCHAMP_WAVES
    string fn;
    if ($value$plusargs("dumpfile=%s", fn)) $dumpfile(fn);
    else $dumpfile("tb_tick_timer.vcd");
    $dumpvars(0, tb_tick_timer);
`endif
    rst_n = 0; repeat (3) @(negedge clk); rst_n = 1; en = 1;

    for (int i = 1; i <= 3 * TPP + 5; i++) begin
      @(posedge clk); #1;
      if (pulse !== ((i % TPP) == 0)) begin
        errors++;
        $display("CHIPCHAMP_FAIL: pulse=%b at enabled cycle %0d", pulse, i);
      end
    end

    // pause: pulse must stay low while disabled
    en = 0;
    repeat (4) begin
      @(posedge clk); #1;
      if (pulse !== 1'b0) begin
        errors++;
        $display("CHIPCHAMP_FAIL: pulse while disabled");
      end
    end

    if (errors == 0) $display("CHIPCHAMP_PASS: tick_timer ok (%0d pulses)", 3);
    else             $display("CHIPCHAMP_FAIL: %0d error(s)", errors);
    $finish;
  end

  initial begin
    #100000;
    $display("CHIPCHAMP_FAIL: watchdog timeout");
    $finish;
  end
endmodule
