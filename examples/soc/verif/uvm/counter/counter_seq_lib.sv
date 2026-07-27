class counter_base_seq extends uvm_sequence#(counter_seq_item);
  `uvm_object_utils(counter_base_seq)
  rand int unsigned n_items = 20;
  constraint c_n { n_items inside {[10:50]}; }

  function new(string name = "counter_base_seq");
    super.new(name);
  endfunction

  task body();
    repeat (n_items) begin
      req = counter_seq_item::type_id::create("req");
      start_item(req);
      if (!req.randomize())
        `uvm_error("RANDFAIL", "seq_item randomization failed")
      finish_item(req);
    end
  endtask
endclass
