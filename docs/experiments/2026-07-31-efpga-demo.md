# 2026-07-31 — the eFPGA demo: an FPGA from thin air, closed by the model

`demo_efpga.py` spans the widest arc the platform can currently demonstrate in
one sitting: FABulous generates an FPGA *as RTL* (the fabric), a brand-new
user design is written on the spot, and yosys → nextpnr → bit_gen turn it
into a routed bitstream for that fabric — closing rung E
(`fabric_generated`, `bitstream_generated`) through a real `report.done`.
Same two-mode contract as the waveform demo: scripted and agentic, each
labelled on screen as what it is, twice.

The user design is written **during** the demo rather than shipped with it.
That is what makes the story honest — the bitstream proves a design that did
not exist a minute earlier now runs on an FPGA that did not exist either —
and the edit is also what classifies the task `efpga-fabulous` and puts the
E gates on the ladder.

Raw records: `2026-07-31-efpga-demo.json` (the `demo-efpga-agentic`
experiments).

## What building it surfaced, before any run

**The `efpga-fabulous` task class was unreachable.** It sat in
`TASK_CLASSES`, its rung-E gates were fully implemented and live-tested — but
no branch of `classify()` ever produced it. An edit inside a FABulous project
classified as plain `rtl_functional`, whose gates (smoke test, regression,
coverage baseline) are unpayable for a fabric-mapped design. `report.done`
could therefore never accept eFPGA work at all, scripted or agentic. The fix
is a classification branch on FABulous project content (`user_design/`,
`fabric.csv`, `Tile/*.csv|.list`); plain-RTL classification verified
untouched, and the full suite run over the change.

**The install had been dead since the autocode era.** The FABulous package
survived in the old venv, but its console scripts' shebangs pointed at
`/home/riadh/autocode/...` — venv scripts do not survive a directory rename —
and the bitstream flow additionally needs `task` (go-task). Repaired with
three shims in chipchamp's own venv (`FABulous`, `bit_gen`, `task`); the
"live" closure-matrix rows were true when written and had silently rotted
since.

**The wrapper is part of the contract.** FABulous instantiates the user
design *by module name* inside `user_design/top_wrapper.v` (the pad ring +
global clock). Programming a new design means retargeting that instance —
skipping it fails synthesis on an unresolved module. Both demo modes do it;
the agentic prompt states it.

## Mode 1 — scripted: green

    fabric      J-0354 · 55 HDL files · ~6s     (Fabric/eFPGA.v, 410 KB)
    write       lfsr16.v — a 16-bit maximal LFSR, written by the demo
    retarget    top_wrapper.v → lfsr16
    ladder      E efpga-fabulous 1/2 ✓ fabric_generated
    bitstream   J-0357 · 12,024 B · routed=True · fmax 75.19 MHz · ~5s
    ladder      2/2 → report.done (efpga-fabulous): ACCEPTED

The fmax tells its own story: the shipped counter routes at 29.94 MHz, the
LFSR at 75.19 MHz — genuinely different logic on the same fabric (the
bitstream is constant-size because it configures the whole fabric).

## Mode 2 — agentic: qwen3.6:35b, two runs

| run | steps | outcome | arc |
|---|---|---|---|
| 1 | 32 (capped) | **4/5** — everything but the claim | ~12 steps of orientation (yaml-hunting, CSV greps), then flawless execution: fabric, authored RTL that synthesized *first try*, unprompted wrapper retarget, bitstream — then a polish loop (4 design revisions, each dutifully re-proven with a fresh bitstream, 5 passing builds). Its final step was `evidence.bundle` — **it signed bundle B-1785490705 with the gate table 2/2 green inside, and ran out of budget one step before `report.done`**. It did the paperwork and had no step left to file it |
| 2 | 40 budget, used 29 | **5/5 — completed** | direct: loaded only the 4 eFPGA tools, fabric, authored `lfsr_16bit_fib.v`, hit a build failure on its own code, debugged it, re-proved, and closed. Kept a live `plan.update` checklist (×5) and ran `policy.check` before claiming, unprompted. Its first `report.done` did not take — it had edited the design after its last proof — so it **re-proved (J-0367) and claimed again**: fix-then-reprove-then-claim, which is exactly the discipline the gates exist to enforce |

Run 1's near-miss is worth keeping verbatim in the record because the
behaviour that killed it is the behaviour the platform wants: it *never let
its evidence go stale* — every design revision was followed by a fresh
bitstream rather than a close on old proof. It over-verified an
already-proven design three times; the failure was executive, not epistemic.

## Step-budget calibration (same model across both demos)

    16 steps  → capped mid-investigation (waveform run 11)
    32 steps  → knife-edge: one close at 30, one cap at 32 mid-ceremony
    40 steps  → closed with 11 to spare

40 is the sane default for qwen3.6:35b-a3b on this machine.

## The campaign's two autonomous closes, side by side

| demo | skill demonstrated | steps | wall |
|---|---|---|---|
| waveform | **debugging** — read a failure into a one-line RTL fix | 30 | 334s |
| eFPGA | **authoring** — write new RTL, program it onto a generated fabric | 29 | 641s |

Same model, same loop, both closed through `report.done` on evidence. The
walls removed along the way (teaching errors, `accepts` hints, bare-JSON
recovery, scalar coercion, mid-task `tools.load`, the ladder as a to-do
list) were each load-bearing in both closes.

## Still open on this vertical

The demo stops at the bitstream. The two extensions ranked in the demo
proposal remain: hardening the fabric to GDSII (`efpga-fabulous.harden` is
wired but has never run live here — schedule its first run as its own
recorded experiment before promising it on a stage), and
bitstream-vs-RTL equivalence by simulating the *programmed* fabric
(`run_simulation`), which would turn "we generated a bitstream" into two
matching waveforms.
