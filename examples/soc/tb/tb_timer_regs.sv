// Self-checking testbench for the GENERATED timer register block (P10/US-09):
// APB write/readback of RW fields, RO hw-value readthrough, W1C set/clear,
// and pslverr on an unmapped address.
`timescale 1ns/1ps
module tb_timer_regs;
  logic pclk = 0, presetn = 0;
  logic psel = 0, penable = 0, pwrite = 0;
  logic [11:0] paddr = '0;
  logic [31:0] pwdata = '0;
  logic [31:0] prdata;
  logic pready, pslverr;
  logic        hwif_ctrl_en_o;
  logic [7:0]  hwif_ctrl_prescale_o;
  logic [31:0] hwif_count_value_i = '0;
  logic        hwif_status_ovf_set_i = 0;

  int errors = 0;

  timer_reg_top dut (.*);

  always #5 pclk = ~pclk;

  task automatic apb_write(input [11:0] a, input [31:0] d);
    @(negedge pclk); psel = 1; pwrite = 1; paddr = a; pwdata = d;
    @(negedge pclk); penable = 1;
    @(negedge pclk); psel = 0; penable = 0; pwrite = 0;
  endtask

  task automatic apb_read(input [11:0] a, output [31:0] d, output err);
    @(negedge pclk); psel = 1; pwrite = 0; paddr = a;
    @(negedge pclk); penable = 1;
    #1; d = prdata; err = pslverr;
    @(negedge pclk); psel = 0; penable = 0;
  endtask

  task automatic check(input [31:0] got, input [31:0] exp, input string what);
    if (got !== exp) begin
      errors++;
      $display("CHIPCHAMP_FAIL: %s got=%h exp=%h @%0t", what, got, exp, $time);
    end
  endtask

  logic [31:0] rd; logic err;

  initial begin
`ifdef CHIPCHAMP_WAVES
    string fn;
    if ($value$plusargs("dumpfile=%s", fn)) $dumpfile(fn);
    else $dumpfile("tb_timer_regs.vcd");
    $dumpvars(0, tb_timer_regs);
`endif
    repeat (3) @(negedge pclk); presetn = 1;

    // reset values
    apb_read(12'h000, rd, err);
    check(rd, 32'h0000_0400, "CTRL reset (PRESCALE=4, EN=0)");

    // RW write + readback + hwif
    apb_write(12'h000, 32'h0000_2701);
    apb_read(12'h000, rd, err);
    check(rd, 32'h0000_2701, "CTRL readback");
    check({31'h0, hwif_ctrl_en_o}, 32'h1, "hwif EN");
    check({24'h0, hwif_ctrl_prescale_o}, 32'h27, "hwif PRESCALE");

    // RO readthrough from hw
    hwif_count_value_i = 32'hDEAD_BEEF;
    apb_read(12'h004, rd, err);
    check(rd, 32'hDEAD_BEEF, "COUNT RO readthrough");

    // W1C: hw set, sw clear
    @(negedge pclk); hwif_status_ovf_set_i = 1;
    @(negedge pclk); hwif_status_ovf_set_i = 0;
    apb_read(12'h008, rd, err);
    check(rd, 32'h1, "STATUS.OVF set by hw");
    apb_write(12'h008, 32'h1);
    apb_read(12'h008, rd, err);
    check(rd, 32'h0, "STATUS.OVF cleared by W1C");

    // unmapped address -> pslverr
    apb_read(12'hFFC, rd, err);
    check({31'h0, err}, 32'h1, "pslverr on unmapped");

    repeat (2) @(negedge pclk);
    if (errors == 0) $display("CHIPCHAMP_PASS: timer_reg_top ok");
    else             $display("CHIPCHAMP_FAIL: %0d error(s)", errors);
    $finish;
  end

  initial begin #100000; $display("CHIPCHAMP_FAIL: watchdog"); $finish; end
endmodule
