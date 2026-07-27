# SoC example — design conventions

- **Reset**: async assert / sync deassert, active-low `rst_n`. No internal resets on datapath registers.
- **Clocks**: `clk_core` (core domain), `clk_io` (I/O domain). Cross with a two-flop
  synchronizer (`push_meta`→`push_sync` idiom) for single-bit control only.
- **CDC**: single-bit control crossings only in this example. Multi-bit crossings
  must use an async FIFO (not present here).
- **FSMs**: enum-typed `state`/`next`, `always_ff` register + `always_comb` next-state,
  `unique case`, `default` arm present.
- **No latches. No `casex`.** Active-low signals use the `_n` suffix.
- **Style**: lint-clean on Verilator `-Wall` and the project Verible ruleset.
