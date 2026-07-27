class counter_base_test extends uvm_test;
  `uvm_component_utils(counter_base_test)
  counter_env env;

  function new(string name = "counter_base_test", uvm_component parent = null);
    super.new(name, parent);
  endfunction

  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    env = counter_env::type_id::create("env", this);
  endfunction

  task run_phase(uvm_phase phase);
    counter_base_seq seq;
    phase.raise_objection(this);
    seq = counter_base_seq::type_id::create("seq");
    if (!seq.randomize()) `uvm_error("RANDFAIL", "seq randomization failed")
    seq.start(env.agt.sqr);
    #100ns;
    phase.drop_objection(this);
  endtask
endclass

class counter_smoke_test extends counter_base_test;
  `uvm_component_utils(counter_smoke_test)
  function new(string name = "counter_smoke_test", uvm_component parent = null);
    super.new(name, parent);
  endfunction
endclass
