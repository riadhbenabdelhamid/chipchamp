# APB protocol pack

Chipchamp protocol knowledge pack (SPEC §15.3) for AMBA APB (APB2/3 essentials).

## Contents

| File | Purpose |
|---|---|
| `apb_checker.sv` | Bindable protocol checker. `ASSUME_MASTER=1` for formal slave verification (master rules become assumptions on free inputs); `ASSUME_MASTER=0` as a simulation monitor (everything asserted). Portable formal style — works in the open Yosys/SymbiYosys flow, no Verific needed. |
| `formal/apb_regblock_formal.sv` | Formal harness binding the checker to a generated `timer_reg_top` (the regmap compiler's output) — proven with `sby` BMC as part of the test suite. |

## Properties

- **M1** SETUP (`psel & !penable`) is followed by ACCESS (`psel & penable`)
- **M2** `paddr`/`pwrite` stable across the transfer; `pwdata` stable for writes
- **M3** after a completed ACCESS (`pready`), `penable` deasserts
- **S1** slave completes every access (`pready` during ACCESS — always-ready slaves)
- **S2** `pslverr` only during ACCESS
- Covers: a completed read and a completed write (non-vacuity)

## Known pitfalls this pack catches

- Read data taken in SETUP instead of ACCESS phase (master sampling too early)
- Address/control glitching between SETUP and ACCESS
- `penable` held high across back-to-back transfers (must return low)
- Slave raising `pslverr` outside the access window
- Wait-state slaves advertised as always-ready (S1 fails — use a wait-state
  variant of the checker before signoff on such slaves)

## Usage

Simulation monitor (bind into your TB):

```systemverilog
bind my_apb_slave apb_checker #(.ADDR_W(12), .ASSUME_MASTER(0)) u_apb_chk (.*);
```

Formal (SymbiYosys), letting the assumptions drive a compliant master:

```
[script]
read -formal apb_checker.sv
read -formal my_slave.sv
read -formal my_harness.sv
prep -top my_harness
```
