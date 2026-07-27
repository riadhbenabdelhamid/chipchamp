// APB protocol checker — Chipchamp protocol pack (SPEC §15.3).
//
// Portable formal style: clocked immediate assertions with $past, which the
// open Yosys/SymbiYosys flow supports without a Verific license. The same
// checker binds into simulation (assertions fire as $error).
//
// Roles:
//   ASSUME_MASTER=1  (formal, checking a SLAVE): master-side protocol rules
//                    become assumptions constraining the free inputs; slave
//                    responses are asserted.
//   ASSUME_MASTER=0  (simulation monitor): everything is asserted.
//
// Protocol subset checked (APB2/3 essentials):
//   M1  SETUP (psel & !penable) is followed by ACCESS (psel & penable)
//   M2  paddr/pwrite stable from SETUP through ACCESS; pwdata stable for writes
//   M3  after a completed ACCESS (pready), penable deasserts
//   S1  slave asserts pready during ACCESS (this pack targets always-ready slaves)
//   S2  pslverr may only be raised during ACCESS
module apb_checker #(
    parameter int ADDR_W = 12,
    parameter bit ASSUME_MASTER = 0
) (
    input logic              pclk,
    input logic              presetn,
    input logic              psel,
    input logic              penable,
    input logic              pwrite,
    input logic [ADDR_W-1:0] paddr,
    input logic [31:0]       pwdata,
    input logic [31:0]       prdata,
    input logic              pready,
    input logic              pslverr
);
  logic f_past_valid;
  initial f_past_valid = 1'b0;
  always @(posedge pclk) f_past_valid <= 1'b1;

  generate
    if (ASSUME_MASTER) begin : g_master_assumed
      always @(posedge pclk) begin
        if (f_past_valid && presetn && $past(presetn)) begin
          // M1: setup -> access
          if ($past(psel && !penable))
            assume (psel && penable);
          // M2: control stability across the transfer
          if ($past(psel && !penable)) begin
            assume (paddr  == $past(paddr));
            assume (pwrite == $past(pwrite));
            if (pwrite)
              assume (pwdata == $past(pwdata));
          end
          // M3: access completes -> enable falls
          if ($past(psel && penable && pready))
            assume (!penable);
        end
        // no access out of reset
        if (!presetn)
          assume (!psel && !penable);
      end
    end else begin : g_master_asserted
      always @(posedge pclk) begin
        if (f_past_valid && presetn && $past(presetn)) begin
          if ($past(psel && !penable))
            assert (psel && penable);
          if ($past(psel && !penable)) begin
            assert (paddr  == $past(paddr));
            assert (pwrite == $past(pwrite));
            if (pwrite)
              assert (pwdata == $past(pwdata));
          end
          if ($past(psel && penable && pready))
            assert (!penable);
        end
      end
    end
  endgenerate

  // Slave-side obligations (always asserted)
  always @(posedge pclk) begin
    if (f_past_valid && presetn) begin
      // S1: always-ready slave completes every access
      if (psel && penable)
        assert (pready);
      // S2: pslverr only during the access phase
      if (pslverr)
        assert (psel && penable);
    end
  end

  // Reachability covers: prove the properties aren't vacuous
  always @(posedge pclk) begin
    if (f_past_valid && presetn) begin
      cover (psel && penable && pwrite && pready);   // a completed write
      cover (psel && penable && !pwrite && pready);  // a completed read
    end
  end
endmodule
