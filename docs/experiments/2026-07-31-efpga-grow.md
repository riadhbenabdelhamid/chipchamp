# 2026-07-31 — eFPGA grow (E1): the design doesn't fit, so grow the chip

`demo_efpga_grow.py`. The inversion of the triage question: the design is a
hard REQUIREMENT — 32 independent 16-bit LFSRs, 512 flops — and the chip is
simply too small. Placement dies at 688 LUT+FF BELs with `no BELs remaining`.
The software reflex is to shrink the program; the semiconductor move is to
grow the silicon: `fabric.csv` is a floor plan, +8 rows of logic tiles is a
CSV edit, and the regenerated 1,072-BEL fabric routes the unchanged design
(18,424-byte bitstream vs the stock fabric's 12,024 — the chip literally got
bigger). Raw records: `2026-07-31-efpga-grow.json`.

## Engineering the premise

Probed on the real flow: 24 LFSRs route on stock silicon, 28 overflow — so
N=32 is decisively infeasible, not knife-edge. Growth discipline: insert
whole DSP_top/DSP_bot row-pairs (the supertile must stay paired) and only
above the south termination, so every IO coordinate the wrapper pins
(X0Y1..X0Y14, counted from the north edge) keeps its meaning. First grown
bitstream immediately flushed out a latent harness bug: `Test/makehex.py`
asserted bitstreams fit 16,384 bytes — a capacity assumption nobody had ever
violated because nobody had ever grown the fabric (now 65,536).

Scripted mode: green first try. Overflow → floor-plan edit → regen →
routed → ladder E 2/2 → `report.done` ACCEPTED.

## Agentic runs (blind: notebook + job archive sequestered)

| # | experiment | result | arc |
|---|---|---|---|
| 1 | E-26335e482d | 4/7 capped, 57 min | reproduced, design untouched, fabric regenerated — then the **harden tar pit** (its second victim): ran `harden` twice, and the broken flow's yosys crash became "the -y issue blocks PnR", a flow that isn't even on the bitstream path. Fix: the adapter now names the incompatibility and its non-effect on routing |
| 2 | E-039080fc72 | 3/7 capped | never saw the overflow: passed the **wrapper as the design** (`Re-definition of module top_wrapper`), and the summary "bitstream FAILED: 0 bytes" hid the reason, so the Global_Clock ghost hunt followed. Three fixes: argument guards on the bitstream tool, decisive fail reasons in failed summaries, and the grader's `reproduced` axis now demands the overflow signature |
| 3 | E-f9c24990c5 | **5/7** | the judgment run: real overflow reproduced, design untouched, **correctly chose to edit `fabric.csv`**, regenerated. Lost on mechanics — a from-imagination full-file rewrite (dropped the Parameters section, invented tile names) and a token-stall trying to reproduce the whole CSV. `fs.write` now warns when a rewrite halves a substantial file and points at `fs.edit` |

A process-death mid-campaign (session restart killed run 2's first attempt
before its `finally`) cost the notebook its original entries and forced a
manual workspace recovery — snapshots now live on disk (`.presnap`,
`.preblind`) and both eFPGA demos start with `recover_stale_state()`, so a
dead run heals on the next launch instead of by hand.

## Where it stands

The verdict ladder across runs — 4/7 (tar pit), 3/7 (argument spiral), 5/7
(right idea, botched edit) — tracks exactly with which trap was disarmed
last. All judgment axes have now been individually demonstrated; the
remaining wall is qwen3.6:35b's structured-CSV editing under a token budget:
surgical `fs.edit` inserts beat full-file rewrites, and the model reached
for the rewrite. E1 has not yet been closed 7/7 by a model; the scripted
close and run 3's 5/7 bound the gap tightly.
