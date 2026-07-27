package counter_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  `include "counter_seq_item.sv"
  `include "counter_sequencer.sv"
  `include "counter_driver.sv"
  `include "counter_monitor.sv"
  `include "counter_agent.sv"
  `include "counter_scoreboard.sv"
  `include "counter_env.sv"
  `include "counter_seq_lib.sv"
  `include "counter_test_lib.sv"
endpackage
