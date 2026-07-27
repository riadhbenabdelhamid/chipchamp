// Formal harness: APB protocol pack vs the GENERATED timer register block.
// The checker assumes a compliant master (free inputs constrained) and asserts
// the slave obligations of timer_reg_top. Proven with SymbiYosys (BMC).
module apb_regblock_formal (
    input logic        pclk,
    input logic        psel,
    input logic        penable,
    input logic        pwrite,
    input logic [11:0] paddr,
    input logic [31:0] pwdata,
    input logic [31:0] hwif_count_value_i,
    input logic        hwif_status_ovf_set_i
);
  // deterministic internal reset (2 cycles) so BMC starts from a known state
  logic [1:0] rst_cnt = 2'b00;
  always @(posedge pclk)
    if (!rst_cnt[1]) rst_cnt <= rst_cnt + 2'b01;
  wire presetn = rst_cnt[1];

  logic [31:0] prdata;
  logic pready, pslverr;
  logic hwif_ctrl_en_o;
  logic [7:0] hwif_ctrl_prescale_o;

  timer_reg_top dut (
    .pclk(pclk), .presetn(presetn),
    .psel(psel), .penable(penable), .pwrite(pwrite),
    .paddr(paddr), .pwdata(pwdata), .prdata(prdata),
    .pready(pready), .pslverr(pslverr),
    .hwif_ctrl_en_o(hwif_ctrl_en_o),
    .hwif_ctrl_prescale_o(hwif_ctrl_prescale_o),
    .hwif_count_value_i(hwif_count_value_i),
    .hwif_status_ovf_set_i(hwif_status_ovf_set_i));

  apb_checker #(.ADDR_W(12), .ASSUME_MASTER(1)) chk (
    .pclk(pclk), .presetn(presetn),
    .psel(psel), .penable(penable), .pwrite(pwrite),
    .paddr(paddr), .pwdata(pwdata), .prdata(prdata),
    .pready(pready), .pslverr(pslverr));

  // Regblock-specific formal property: an APB write to CTRL is observable on
  // the hardware interface the following cycle (the generated RW path works).
  always @(posedge pclk) begin
    if (presetn && $past(presetn))
      if ($past(psel && penable && pwrite && pready && (paddr[11:2] == 10'd0)))
        assert (hwif_ctrl_en_o == $past(pwdata[0]));
  end
endmodule
