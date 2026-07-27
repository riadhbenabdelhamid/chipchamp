class counter_scoreboard extends uvm_subscriber#(counter_seq_item);
  `uvm_component_utils(counter_scoreboard)
  int unsigned n_seen;

  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction

  function void write(counter_seq_item t);
    n_seen++;
    // TODO reference model: compute expected outputs from t's inputs and
    // compare — `uvm_error on mismatch. The monitor already samples
    // count, tc.
  endfunction

  function void report_phase(uvm_phase phase);
    if (n_seen == 0)
      `uvm_error("SB_EMPTY", "scoreboard saw no transactions")
    else
      `uvm_info("SB", $sformatf("%0d transactions observed", n_seen), UVM_LOW)
  endfunction
endclass
