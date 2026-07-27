class counter_monitor extends uvm_monitor;
  `uvm_component_utils(counter_monitor)
  virtual counter_if vif;
  uvm_analysis_port#(counter_seq_item) ap;

  function new(string name, uvm_component parent);
    super.new(name, parent);
    ap = new("ap", this);
  endfunction

  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    if (!uvm_config_db#(virtual counter_if)::get(this, "", "vif", vif))
      `uvm_fatal("NOVIF", "no virtual interface for monitor")
  endfunction

  task run_phase(uvm_phase phase);
    counter_seq_item tr;
    forever begin
      @(posedge vif.clk);
      if (vif.rst_n != 1'b1) continue;
      tr = counter_seq_item::type_id::create("tr");
      tr.en = vif.en;
      tr.clr = vif.clr;
      tr.count = vif.count;
      tr.tc = vif.tc;
      ap.write(tr);
    end
  endtask
endclass
