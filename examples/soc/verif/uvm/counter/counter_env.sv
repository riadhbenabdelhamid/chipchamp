class counter_env extends uvm_env;
  `uvm_component_utils(counter_env)
  counter_agent      agt;
  counter_scoreboard sb;

  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction

  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    agt = counter_agent::type_id::create("agt", this);
    sb  = counter_scoreboard::type_id::create("sb", this);
  endfunction

  function void connect_phase(uvm_phase phase);
    agt.mon.ap.connect(sb.analysis_export);
  endfunction
endclass
