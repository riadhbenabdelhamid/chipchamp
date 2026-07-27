class counter_driver extends uvm_driver#(counter_seq_item);
  `uvm_component_utils(counter_driver)
  virtual counter_if vif;

  function new(string name, uvm_component parent);
    super.new(name, parent);
  endfunction

  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    if (!uvm_config_db#(virtual counter_if)::get(this, "", "vif", vif))
      `uvm_fatal("NOVIF", "no virtual interface for driver")
  endfunction

  task run_phase(uvm_phase phase);
    vif.en <= '0;
    vif.clr <= '0;
    @(posedge vif.rst_n iff vif.rst_n == 1'b1);
    forever begin
      seq_item_port.get_next_item(req);
      @(posedge vif.clk);
      vif.en <= req.en;
      vif.clr <= req.clr;
      seq_item_port.item_done();
    end
  endtask
endclass
