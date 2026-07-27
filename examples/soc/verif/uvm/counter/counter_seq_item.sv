class counter_seq_item extends uvm_sequence_item;
  rand logic              en;
  rand logic              clr;
  logic        [7:0] count;
  logic              tc;
  `uvm_object_utils_begin(counter_seq_item)
    `uvm_field_int(en, UVM_ALL_ON)
    `uvm_field_int(clr, UVM_ALL_ON)
    `uvm_field_int(count, UVM_ALL_ON)
    `uvm_field_int(tc, UVM_ALL_ON)
  `uvm_object_utils_end

  function new(string name = "counter_seq_item");
    super.new(name);
  endfunction
  // TODO: add protocol constraints (legal opcodes, aligned addresses, ...)
endclass
