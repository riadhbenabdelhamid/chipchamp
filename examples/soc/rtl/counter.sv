// Parameterizable saturating/​wrapping up-counter with synchronous clear.
module counter #(
    parameter int WIDTH = 8,
    parameter bit WRAP  = 1
) (
    input  logic              clk,
    input  logic              rst_n,
    input  logic              en,
    input  logic              clr,
    output logic [WIDTH-1:0]  count,
    output logic              tc      // terminal count
);
  logic [WIDTH-1:0] count_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)       count_q <= '0;
    else if (clr)     count_q <= '0;
    else if (en) begin
      if (count_q == {WIDTH{1'b1}})
        count_q <= WRAP ? '0 : count_q;
      else
        count_q <= count_q + 1'b1;
    end
  end

  assign count = count_q;
  assign tc    = en & (count_q == {WIDTH{1'b1}});
endmodule
