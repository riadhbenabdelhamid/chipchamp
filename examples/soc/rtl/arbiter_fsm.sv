// Two-requester round-robin arbiter, explicit FSM (Moore-style grants).
module arbiter_fsm (
    input  logic clk,
    input  logic rst_n,
    input  logic req0,
    input  logic req1,
    output logic gnt0,
    output logic gnt1
);
  typedef enum logic [1:0] {
    IDLE  = 2'b00,
    SERVE0 = 2'b01,
    SERVE1 = 2'b10
  } state_e;

  state_e state, next;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) state <= IDLE;
    else        state <= next;
  end

  always_comb begin
    next = state;
    unique case (state)
      IDLE: begin
        if (req0)      next = SERVE0;
        else if (req1) next = SERVE1;
      end
      SERVE0: begin
        if (req1)      next = SERVE1;
        else if (!req0) next = IDLE;
      end
      SERVE1: begin
        if (req0)      next = SERVE0;
        else if (!req1) next = IDLE;
      end
      default: next = IDLE;
    endcase
  end

  assign gnt0 = (state == SERVE0);
  assign gnt1 = (state == SERVE1);
endmodule
