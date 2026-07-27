# Spec closure matrix

Status of every gap that was called out in the earlier "spec-but-not-built"
analysis, after the gap-closure pass. **Live** = exercised against real tools in
the test suite; **Fixture** = normalizer/logic validated against captured output,
adapter ready for a customer machine; **Dry-run** = command construction tested
without the scheduler; **N/A** = organizational, not code.

| Spec area | Was | Now | Evidence |
|---|---|---|---|
| Production SV front end (§8.3) | pragmatic regex parser (silently failed on real RTL) | **built** | `design/slang_frontend.py` via **pyslang/slang**; DesignDB uses it when available, pragmatic fallback. Shakeout: **ibex 176 modules / ~6 s / 0 warnings**, real FSMs + hierarchy; regex parser returned empty ports on the same code |
| Multi-agent orchestration (§8.10) | absent | **built** | `agent/orchestrator.py` + `roles.py`; least-privilege roles, shared license pool (Live test proves peak=1 seat), orchestrator-only `report.done` (FR-MA-03), `triage --agents` |
| MCP server + client (§15.2) | absent | **built** | `mcp/server.py` (`chipchamp mcp-serve`) + `mcp/client.py`; Live subprocess round-trip test |
| STA + timing gate (§9-H) | absent | **built** | OpenSTA adapter (Fixture parser) + Yosys `ltp` structural proxy (Live); `sta.run`/`sta.paths`; `sta_delta` → timing gate |
| Register map (§9-I, P10) | gate-only | **built** | `regmap/compiler.py` YAML→APB SV+H+MD (deterministic); generated block passes its APB TB (Live) and APB formal pack (Live) |
| Source-of-truth map (§12.5, FR-PROJ-04) | absent | **built** | `[[generated]]` in config; `fs.write` on generated files refused (Live test); `regmap_regen` gate byte-compares |
| Waivers as data (§12.6, FR-ADPT-03) | pass-through | **built** | `policy/waivers.py`; only active+approved+unexpired suppress; `lint.waive` drafts to staging; lint gate counts unwaived (Live) |
| Commercial EDA adapters (§8.4) | absent | **fixture** | Questa, VCS sim + PrimeTime STA normalizers (Fixture tests); license features + manifests; honest "live pending" |
| Farm connectors (§8.5) | local-only | **dry-run** | `jobs/farm.py` LSF/SLURM wrapping (`bsub -K`/`srun`); Dry-run tests; degrades to local with a note |
| FST/FSDB waves (§8.6) | VCD-only | **FST built** | `fst2vcd` bridge; Live FST round-trip test. FSDB remains M2 (needs Verdi) |
| FuseSoC/Bender ingestion (§12.2) | absent | **built** | `filelist.py` `parse_fusesoc_core`/`parse_bender_manifest`; tests |
| env-modules (§12.2, FR-PROJ-03) | recorded-only | **built** | `jobs/runner.py` `wrap_with_modules` (login-shell `module load`); test |
| cocotb runner (P4) | absent | **built** | `adapters/cocotb_adapter.py`; Live coroutine TB over Icarus (1 passed), results.xml parse (Fixture) |
| Prompt-injection flagging (§13.4, FR-SEC-05) | absent | **built** | `policy/injection.py`; every tool result scanned + quarantined; red-team test |
| MR / autonomy actions (§11.2) | levels-only | **built** | `mr.prepare` tool: branch+commit+MR body+bundle, refused below L2 (Live test) |
| Model routing (§14) | single-model | **built** | `[model.routing]` role→provider:model; subagents route to cheaper models; test |
| Protocol/methodology packs (§15.3) | absent | **APB built** | `packs/apb/` checker + formal harness; Live sby proof + mutant-fails-proof; `chipchamp packs` |
| User-authored skills (§15.3) | absent | **built** | `chipchamp/skills.py`: SKILL.md (Agent Skills format) discovery over workspace/config/registered dirs (`chipchamp skills add/list/remove/show`); compact prompt index + `skill.use`/`skill.read` tools (path-confined, injection-scanned); `/skill <name>` + direct `/<name>` in the REPL (slash menu tags them `skill · …` in violet; `/skill:<name>` reaches one shadowed by a built-in); Live test against the ~/skills corpus (97 skills) |
| ChipchampBench (§17.1) | absent | **B1/B6 built** | `bench/`; Live B1 mutation-detection on real sims; `chipchamp bench` JSON reports |
| Red-team evals (§17.4) | absent | **built** | `tests/test_redteam.py`: 7 adversarial scenarios as executable tests |
| CI/repo bot (§7.3) | absent | **built** | `chipchamp ci` headless gate (nonzero exit) + `.github/workflows/chipchamp.yml` |
| Web dashboard (§7.5) | absent | **built** | `dashboard.py` static HTML from job records+bundles (no source); `chipchamp dashboard` |
| Python SDK (§7.4) | importable | **documented** | package is scriptable (`demo.py`, `bench/harness.py` use it as a library) |

## Beyond the original spec: RTL-to-GDSII (physical vertical)

Not in the original spec at all (NG4 explicitly stopped at synth+STA), added later:

| Capability | Status | Evidence |
|---|---|---|
| RTL-to-GDSII flow | **live** | `librelane` adapter drives LibreLane (OpenROAD/Yosys/Magic/KLayout/Netgen) on sky130; example `counter` → 213 KB GDSII, signoff-clean |
| Physical signoff gates (§11-style) | **live** | `physical` task class: DRC-clean / LVS-clean / timing-met (all corners) / antenna-clean, read from LibreLane `metrics.json` |
| Physical query tools | **live** | `pd.run`, `pd.metrics`, `pd.drc` (+ `chipchamp pnr`) — query the ~300-metric `metrics.json`, not the GDS/DEF |

Still physical-side open: commercial PnR (Innovus/ICC2) + signoff (Calibre/StarRC) as fixture adapters; foundry (NDA) PDKs via the on-prem/air-gap path; physical timing-closure ECO playbooks; cross-probing GDS instances back to RTL.

## Beyond the original spec: the eFPGA-FABulous vertical

| Capability | Status | Evidence |
|---|---|---|
| eFPGA-FABulous fabric generation | **live** | `fabulous` adapter → `run_FABulous_fabric` (55 HDL files, top `Fabric/eFPGA.v`) |
| User-design → bitstream | **live** | Yosys synth → `nextpnr-generic --uarch fabulous` P&R → `bit_gen`; demo `sequential_16bit_en` → 12 KB routed bitstream (job J-0342) |
| eFPGA-FABulous gates | **live** | `efpga-fabulous` task class: `fabric_generated` + `bitstream_generated` (routed), from the FABulous job metrics |
| Fabric hardening → GDSII | **wired** | `efpga-fabulous.harden` → `run_FABulous_eFPGA_macro` (LibreLane) — reuses the physical vertical |

eFPGA-FABulous-side open: on-device bitstream simulation as a verification gate (`run_simulation`); authoring custom fabrics (tiles/switch-matrices) rather than the demo fabric; bitstream-vs-RTL equivalence.

## Beyond the original spec: UVM + functional coverage (UCIS)

| Capability | Status | Evidence |
|---|---|---|
| UVM on the license-free path | **live** | Accellera **uvm-core 2020.3.1** compiles+runs under **Verilator 5** (`--timing -j 0`, `UVM_NO_DPI`); smoke test report summary clean; real log kept as fixture |
| UVM report verdict in job records | **live** | simulator-neutral parser (`adapters/uvm.py`) reads the report-server format from Verilator/Questa/VCS logs; **UVM_ERROR>0 or missing summary fails the job even on exit 0**; wired into icarus/verilator/questa/vcs `parse()` |
| UVM bench scaffolding | **live** | `uvm.scaffold` / `chipchamp uvm <module>`: 13 files (if/item/driver/monitor/agent/env/scoreboard-stub/seqs/tests/tb/filelist) from the design DB; comment-preserving tests.yaml registration; generated bench **elaborates against real 1800.2** (lint test) and **runs end-to-end via sim.run in ~2 min** (`CHIPCHAMP_LIVE_UVM=1`) |
| UCIS functional-coverage ingest | **built** | `coverage/ucis.py` reads UCIS 1.0 XML interchange (Questa/VCS/Xcelium export) → normalized model: holes/attribution/baseline gate identical to code coverage; schema-faithful fixture |
| Functional coverage live on open tools | **live** | cocotb-coverage covergroups over Icarus → XML export (adapter pins path, artifact surfaced) → format-sniffing `cov.load` → `cov.holes` finds the planted `(en=0,clr=1)` hole |

DV-side open: UVM RAL generation from the regmap compiler; multi-agent UVM (virtual sequences across agents); merging per-test UCIS files with test-status attribution from rich history nodes; UVM on Questa/VCS validated live (fixture-tier glue, M2 design-partner activity).

## Beyond the original spec: PPA feedback loop (physical → RTL)

| Capability | Status | Evidence |
|---|---|---|
| Post-route timing → RTL cross-probe | **live** | `pd.critical`: OpenROAD per-corner path reports parsed (25-path real fixture); anonymized flops (`_54_`) recover RTL nets (`count[7]`) from the synthesis netlist; resolve via design DB to module + file:line + fan-in/out cone; worst counter path lands on `counter.sv:27` with per-stage delays |
| Per-module area attribution | **live** | `pd.area`: hierarchy-preserving yosys synth mapped to the ciel sky130 liberty in ~1 s; `$paramod` demangling, instance-count × per-instance rollup; example SoC = 19,570 µm², FIFO the 9.3 kµm² hog |
| PPA snapshot + delta | **live** | `pd.ppa`: pnr-job metrics (+ optional per-module area) → saved baseline → signed deltas annotated better/worse (slack↑ good, area/power↑ bad); self-delta test proves zero-noise |

Physical-side open: automated ECO playbook driving `pd.critical`→RTL-edit→LEC→`pd.ppa` loops; per-instance (not per-definition) area from the DEF; cross-probing DRC violations to layout coordinates→RTL.

## Beyond the original spec: PPA depth (area/power optimization)

| Capability | Status | Evidence |
|---|---|---|
| Activity-driven power | **live** | `pd.power`: workload VCD → `read_power_activities` → OpenSTA `report_power` in the devshell; counter = 231 µW under the real workload, clock tree 28.3%, top-3 consumers are the clock buffers; annotation counts reported (13 vcd-annotated, interior propagated); real report is a fixture; vectorless runs carry an explicit warning |
| Area by RTL line | **live** | `pd.area --by-line`: flop `src` refs exact through mapping; comb cells attributed to the endpoint-register cone; whole-design multi-module merge (`sync_fifo.sv:30` = 8,220 µm² / 311 cells); unattributed remainder reported, never spread |
| Dead-width (coverage × area) | **live** | `pd.deadwidth`: per-bit toggle bins × mapped-netlist flop pricing; finds the FIFO's `wr_data[8:31]`/`rd_data[8:31]` never-toggled bits on the real workload; MSB-contiguous hint vs stuck-interior hint |
| Strategy sweep Pareto | **live** | `pd.sweep`: ABC mapping-script knob (`map -a` vs `map` vs yosys default = 10.7k/16.3k/9.7k µm² on a MAC) + real OpenSTA delay per config; on the FIFO the delay recipe is DOMINATED (area script smaller and faster); pnr mode sweeps LibreLane `SYNTH_STRATEGY` |
| Optimize playbook (P11) | **live** | `chipchamp optimize`: ranked worklist, each item names its `lec.run`-proof loop; missing evidence becomes measurement items |

| Gate-level-sim activity (100% annotation) | **live** | `pd.glsim`: routed netlist + PDK functional models (zero-delay) under the RTL TB (`GL` ifdef) → GDS netlist **passes RTL self-checks** AND the VCD annotates 181/181 pins; boundary-annotation+propagation measured **+79%** vs gate-accurate on the same workload |
| The optimize loop, closed | **live** | `test_optimize_loop.py` (~5 s): coverage → priced dead-width (28 flops) → narrowing edit → **eqy PDR strategy proves the state re-encoding** (k-induction cannot; `[options] depth` was invalid eqy syntax, fixed) → >400 µm² measured → `report.done` accepts on lint+smoke+LEC+no-gaming records. Demo substrate `tick_timer` committed |
| Model-driven session on a worklist item | **demonstrated** | local qwen3.5:9b through the agent loop: plan → `pd.deadwidth` → recovers from missing coverage → `cov.run` → re-check → report (11 steps, real jobs). Flakiness observed is **Ollama server-side tool-call template parsing** (HTTP 500 mid-session, also with gpt-oss) — the platform fails clean with audit; hardening extracted: `plan.update` decodes string-encoded steps |

PPA-depth open: SAIF ingestion (`read_saif` is wired in OpenSTA, no producer on the open path yet); Vivado `report_power -saif` on the FPGA side; automated clock-gating transform (yosys has no production pass — agent RTL edit + LEC is the path); dynamic-vs-leakage corner sweeps; sturdier local-model tool calling (Ollama template issues) or a hosted-model key for long agentic sessions.

## Beyond the original spec: FPGA design checkpoints

| Capability | Status | Evidence |
|---|---|---|
| Open-flow checkpoints (yosys+nextpnr) | **live** | `nextpnr` adapter: post_synth/post_route JSON + report per run; counter → iCE40 hx1k in ~2 s, fmax 365 vs 100 MHz target, checkpoints on disk |
| Vivado design checkpoints (.dcp) | **live** | `vivado` adapter (batch non-project): post_synth/post_place/post_route **.dcp** + timing/util/power/paths reports; counter → xc7a35t under Vivado 2025.1, WNS +2.6 ns @ 200 MHz, 3 DCPs; report normalizers fixture-tested against the real reports |
| FPGA timing → RTL cross-probe | **live** | open flow: nextpnr report segments carry **native RTL source refs** (yosys src attrs survive P&R); Vivado: `count_q_reg[3]/C` → design-DB module + file:line + cone, logic/route split + LUT levels |
| Checkpoint PPA + deltas | **live** | `fpga.ppa`/`fpga.checkpoints`: normalized per-stage snapshots, stage-vs-stage compare, saved baseline → signed better/worse deltas; self-delta = zero noise |
| Flashable bitstream | **live** | `fpga.bitstream`: packs the routed checkpoint via icepack/ecppack/prjoxide (family-dispatched) or Vivado `write_bitstream`; P&R now always writes the packer's input (`--asc`/`--textcfg`/`--fasm`), so packing never re-runs the flow. Both paths proven here: counter → iCE40 = 135 100-byte `.bin` (icepack); counter → xc7a35tcpg236-1 = 2 192 131-byte `.bit` (Vivado 2025.1, `file` reports *Xilinx BIT data … for 7a35tcpg236*) |
| Constraints + pin DRC | **live** | `fpga.run xdc=<file>` feeds `read_xdc` (the flow had none, so a Vivado bitstream was unreachable). Unconstrained ports are the first-bitstream failure — UCIO-1/NSTD-1 are detected and reported as "top-level ports have no pin (LOC) / IOSTANDARD constraints" with the fix, instead of "see the log". chipchamp never downgrades those DRCs: a bitstream with arbitrary pin placement can damage hardware |
| Pin validation + generation, per **board** | **live** | `fpga.pins` / `chipchamp pins`: validates `.xdc`/`.pcf`/`.lpf` against the design DB's real top-level ports (bus-expanded) in ~1 s — typos, unconstrained ports, missing IOSTANDARD, duplicate pins — instead of after synthesis or at bitstream time; exits non-zero for CI. Generates constraints for a board, with `--map port=signal` for bus bases. **Board profiles are authoritative only**: 28 Digilent boards read straight out of Vivado's installed `board_files/**/part0_pins.xml`, plus `--import` of a vendor master constraints file and user profiles in `.chipchamp/boards/*.json`. An unmapped port is emitted as a `# TODO`, **never** assigned a free pin — a wrong pin can short an output. Constraints files are Tcl, so `foreach`/wildcards are reported as *unparsed and unchecked* rather than silently mis-parsed |
| Fit / utilization gate | **live** | `fpga_fits`: per-resource used-vs-available from the **routed** stage (open-flow bels or Vivado's stage-prefixed rows), reporting the tightest resource. Default budget is "must fit" (never a false failure); `[fpga] max_utilization` demands headroom |
| Policy | **live** | `fpga` task class, rung **F** = `fpga_routed`, `fpga_fits`, `fpga_timing_met`, `fpga_bitstream` (vendor WNS or fmax-vs-constraint); `.xdc/.pcf/.lpf` edits classify as `fpga`; eFPGA-FABulous jobs report fmax from the embedded nextpnr log into the same shape |

FPGA-side open: post-place-only checkpoint on the open flow (nextpnr packs+places+routes in one invocation); Vivado incremental flows (`read_checkpoint -incremental`) and DCP-to-DCP ECO; Quartus (.qdb) as a third flow; board programming + hardware-in-the-loop smoke test (an outward action — needs the destructive-tool confirm path); an `fpga.sweep` Pareto to match `pd.sweep`; richer board mapping (Digilent's board-file signal names are IP-integrator style — `led_4bits_tri_o_0` — so a bus map is usually still hand-written); non-Xilinx board profiles all arrive via `--import` since no equivalent installed database exists for the open flow. A Vivado bitstream needs a non-OOC run *and* an XDC; `fpga.bitstream` refuses an out-of-context job up front rather than burning a doomed run. ecp5/machxo2/nexus packing uses the documented packer invocations but is untested here (only ice40 and Vivado were run end to end).

## Beyond the original spec: the RISC-V vertical

A core is only a RISC-V core if it executes the ISA the way the ISA says — and
that claim is checkable mechanically rather than argued in prose.

| Capability | Status | Evidence |
|---|---|---|
| Software toolchain | **live** | `riscv.compile`: C/asm → ELF + `$readmemh` memory image (word-width configurable, little-endian) + disassembly + symbol table, with an explicit `-march`/`-mabi` contract. Verified live on riscv64-unknown-elf-gcc 15.2.0: rv32i/ilp32 build, 6-word image at 0x1000 |
| Toolchain introspection | **live** | `riscv.toolchain` reports triple, version, default ISA, linkable multilibs and which reference model is present, so "can this core be co-simulated here?" is answered before anything is run |
| Reference model | **live** | `riscv.refrun` runs the ELF on spike (canonical, when installed) or the gdb/binutils simulator that ships **with the toolchain**, normalizing both to one trace shape. Bounded by step count *and* wall clock — bare-metal tests conventionally end in a spin loop, so an unbounded model would hang and a plain capture would discard everything it printed |
| ISA co-simulation | **live** | `riscv.cosim` compares a core's retired-instruction trace against the reference's and reports the **first** divergence with PC, instruction and the reference's disassembly, plus the instructions leading in. Core traces are parsed permissively (any line carrying `pc=…`), so a team needs no format change to use it |
| Honest partial results | **live** | agreement over a prefix is reported as a prefix — when either stream ends first, the result says how far the evidence goes instead of implying full equivalence |
| Register-level lockstep | **live** | spike installed here (1.1.1-dev, isolated at `~/.local`), so `--log-commits` supplies the architectural writeback per instruction and cosim compares register values, not just PCs. Spike emits TWO lines per instruction (disassembly + commit) — they are folded into one step, since counting both would double the stream and desync alignment |
| Automatic trace alignment | **live** | a model boots before reaching the program (spike runs a 5-instruction bootrom that jumps to the ELF entry) which a core never executes; `align_traces` finds the offset from the core's first PC. `skip` is per-side (`skip_ref`/`skip_dut`) — applying one count to both streams would drop the core's real instructions and vacuously "match" by comparing nothing |
| ISA compliance (RISCOF) | **wired** | `riscv.compliance` drives `riscof run` on `riscv-arch-test` and reads the per-test verdict from RISCOF's own log line (`<test> : <commit> : Passed\|Failed`), which is stabler than scraping its HTML report. RISCOF is driven as an EXTERNAL tool, not imported: `riscv-config` hard-pins `pyyaml==5.2`, so it lives in its own venv (`~/.local/riscof-venv`, exposed on PATH). Installed and discovered here; a live suite run additionally needs a DUT plugin, which is core-specific |
| Barrel-multithreaded cores | **live** | a barrel core interleaves T hardware threads into ONE retired stream, so comparing it whole against a single-hart model diverges at the second instruction. Traces carry a `hart` (spike's `core N:`, or a testbench's `tid=`/`thread=`/`hart=`), cosim demultiplexes and compares each thread against the model separately, and names the faulty thread instead of returning one blended verdict. `max_steps` is a PER-HART budget — capping the raw parse truncated the later threads and hid bugs past the cut |
| Small / low-memory cores | **live** | `model=spike\|gdbsim\|auto` on refrun/cosim. Spike's **debug module occupies 0x0–0x1000**, so it can never model a core that boots at 0x0 (e.g. BRISKI: 4 KB at 0x0) — the gdb simulator can, via `region="0x0,0x1000"`. Verified live at 0x0 |
| Task class + rung **R** | **live** | a workspace declaring `[riscv]` classifies RTL edits as `riscv` instead of `rtl_functional`; rung R = the V3 functional gates **plus** `isa_cosim` + `isa_compliant`, so choosing it never trades away ordinary RTL rigor. Both ISA gates read machine verdicts recorded on the tool context (the idiom `sta_delta` already uses), never model prose |

RISC-V-side open: a **live** end-to-end RISCOF run — the framework, the suite fetch and the report parsing are wired, but a run needs a DUT plugin for the specific core (`riscof setup --dutname=<core>` scaffolds one), so the parser is verified against RISCOF's real log format rather than a full suite execution; CSR/privilege-aware regmap; driving the core's testbench directly instead of consuming a trace file it already produced; **Driven live against BRISKI** (16-thread barrel core, xsim): a `+trace=` probe in its testbench feeds `riscv.cosim`, and hart 0 and hart 1 each matched the reference model in lockstep over ~60 retired instructions. Getting there established constraints worth recording:

- **spike cannot model a machine with memory below 0x1000.** Its debug module owns `0x0–0x1000` *and* it page-aligns memory regions to 4 KiB, so `-m0x400:0xc00` realigns to `[0x0,0xFFF]` and collides. BRISKI's whole map (ROM 0x0, RAM 0x400) is unreachable — the gdb simulator serves it instead.
- **the gdb simulator has no Zicsr**, and every BRISKI test reads `mhartid` in its prologue. For a barrel core each thread's reference is therefore a build with the hart id materialized (`csrr a0,0xf14` → `li a0,N`) — which is what "one reference stream per hart" means when the model is single-hart.
- `pc_offset` compares a core and a model built at different bases; note that a test hardcoding its data address (`li t0,1024`) does **not** relocate with the linker script, so relocation alone is not a general answer.
- spike's `-p<N>` is wired, but its non-zero harts park in the boot ROM for a bare-metal image, so it does not by itself produce N running streams.

**Also driven live against AbsoLUT** (99-LUT bit-serial RV32I, Verilator): its `SIM_DEBUG` block already exposed the instruction at writeback, so a retire strobe (`state==S_PC && !cnt_nz`, which unlike the regfile-write strobe also covers stores and branches) plus a fetch-latched PC was enough. The official `rv32ui/add` test matched **spike in lockstep over all 430 retired instructions**, PC and instruction word, with `pc_offset` bridging the core's 0x0 map to spike's 0x80000000 and the bootrom auto-skipped.

That run also produced the first case of co-simulation **exonerating the DUT**: against the gdb simulator it diverged at instruction #37, and the disassembly settled it immediately — `0x80000000 + 0xffff8000` must truncate to `0x7fff8000`, the core did that and took the not-equal branch correctly, while the 64-bit gdb simulator did not truncate and fell into the test's fail path. `riscv.refrun` now warns when that model is used as an RV32 reference; spike agreed with the core exactly.

Still open: register-level lockstep on BRISKI (its writeback is not PC-aligned — see the probe comment), the other cores (ALBRISKI/LUMA-RV), and a live RISCOF run.

A structural limitation worth naming: task classification picks ONE class by rank, so a core edit that also trips `cdc`/`timing`/`physical` escalates to that class and its ISA gates drop out. Rung R therefore carries the full V3 functional set, but a change that is simultaneously a CDC change and a core change cannot demand both gate sets today.

## Beyond the original spec: the loop itself (waves 0–4)

The capability matrix above was never the weak axis — the **loop** was. Every
row here closes a gap where the substrate already existed and only the action
was missing.

| Capability | Status | Evidence |
|---|---|---|
| Recorded experiments | **built** | `bench/experiment.py`; `chipchamp --experiment <label> -p …` writes one JSON per run (steps, wall, tokens, context peak, tool tallies, jobs, switches, outcome), `chipchamp experiment autopsy` compares per model. Recording is **passive** — the recorder chains onto the loop's callback so a recorded run behaves exactly like an unrecorded one. `completed` is reserved for an accepted `report.done`; a confident closing paragraph records as `answered` |
| Content-addressed job reuse | **built** | `jobs/cache.py`: `inputs_hash` was computed on every job and used only to print a repro comment. Key = input bytes + argv + the files argv *names* + seed + defines + env modules + adapter version. Governing rule: **a hit must leave the disk indistinguishable from a re-run** — so reuse is confined to verdict-carrying kinds (`lint/elab/cdc/format/sim`) and every artifact the record names *and* the fresh plan expects must exist. Caught by the live optimize-loop test, which priced 28 dead flops at 0.0 µm² when a synth was wrongly reused |
| Undo | **built** | `ctx.edits` held `{old,new}` per path for the gates and nothing could apply it. `/undo` reverts one task, scoped by a per-prompt checkpoint; a file whose contents no longer match what the agent wrote is **kept** and reported — somebody edited it by hand |
| Budgets that hold | **built** | `budgets.py` promised "exceeding a budget *pauses* the task and asks the human"; `can_spend_license`/`can_submit_v3` had **zero call sites**, `record_job` had two `pass` branches, and `model_tokens` had no checker at all — the ceiling was told to the model in the system prompt and otherwise trusted. Checks now run at `ToolContext.submit` and before every model call; a headless run stops at the ceiling, an interactive one may authorize the overrun for that task only |
| Progressive tool disclosure | **built** | `agent/disclosure.py`: all 91 tools shipped every request (~33 KB ≈ 8.4k tokens). Catalog opens at a 31-tool core (read/plan/edit/lint/smoke-sim/read-a-log/report — the bottom of the ladder) and widens by group via `tools.load`. **Measured 8456 → 2080 tokens + a 941-char menu = 72% per request.** Not a capability restriction: `_name_map` still covers everything, so a correctly-named deferred tool works. On by default for ollama/lmstudio |
| Gate ladder as a live HUD | **built** | the rung model was computed only at `report.done`, as a verdict. Now repaints after every job and after a task's first edit, from the **same** `ctx.gate_status()` evidence `report.done` validates — the ladder you watch is the ladder you are judged by. Repaints only when a gate moves; silent until something is edited |
| Flake detection | **built** | `jobs/history.py`: `regression.py` promised seed-stability history but clustered inside one `run()` and discarded it, so only one night was ever visible. Outcomes persist per `(test, seed)`; `chipchamp flaky` reports them and `sim.run` surfaces the verdict inline. Flaky = the **same** seed disagreeing; two seeds disagreeing is a test doing its job |
| Context as a working set | **built** | `agent/working_set.py`: the transcript was append-only, bounded only by `max_steps`. Not summarization — chipchamp's ground truth is external and **addressable**, so an old result collapses to its verdict plus the job id needed to re-fetch the rest (eviction from a cache with a guaranteed backing store). Never evicts user/assistant turns or the recent window; reports `under_budget=false` rather than sacrificing the window to hit a number |
| Detachable jobs + wake | **built** | `submit` blocked while `sim.run` claimed "(async job)". `submit_async` returns a `running` record persisted *before* the thread starts; `wait` returns `None` on timeout (handing back a running record as an answer was the first bug caught). `sim.run(detach=true)` + `job.wait`; a running record can never satisfy a gate. `chipchamp wake` collects a parked session's jobs and continues the task headlessly — from cron or a farm epilogue |
| Model-driven fan-out | **built** | the Orchestrator (§8.10) was reachable only from `triage --agents`. `agent.spawn` exposes it: subagents cannot spawn (depth capped by construction), fan-out bounded at 16, roles routed per capability. FR-MA-02/03 unchanged — one shared license pool, orchestrator-only `report.done` |
| Project notebook | **built** | `notebook.py`: sessions are transcripts, so session #40 knew nothing session #3 learned. Durable findings (root cause, fix pattern, constraint, decision, gotcha) with a digest in every system prompt. Same subject+kind updates in place; notes are removable; framed as prior evidence, not orders |
| Team surface | **built** | `[jobs] store` shares the run store (and therefore the cache) across a team — opt-in, since one person's flaky run becoming everyone's cached verdict is a real consequence. `chipchamp nightly` composes regression + flake history + per-cluster triage fan-out + dashboard, exiting non-zero for cron |

Loop-side open: compaction is size-triggered only (no semantic "this job is
settled" signal); `submit_async` is thread-based, so a detached job dies with
the process that launched it (the farm path would fix that properly); the
notebook is agent- and human-written but nothing prunes stale entries; and
`agent.spawn` fan-out is bounded per call but not per task.

## Deliberately still open (unchanged scope)

- **FSDB waveform reading** — needs the licensed Verdi kit (§8.6 R2); FST covers the open path.
- **Live commercial-tool validation** — Questa/VCS/PrimeTime adapters are fixture-validated;
  validating against the real licensed binaries is an M2 design-partner activity by design.
- **Signoff CDC / lint** (Spyglass/Questa CDC) — CDC-lite (structural) ships; signoff CDC is M2.
- **NFR scale numbers** (§16: 1M-line index in 5 min, 10k tests/night) — the platform runs the
  example at interactive speed; SoC-scale benchmarking needs SoC-scale inputs.
- **SOC 2 / signed-release infrastructure** (§13.5-6) — organizational, not code (N/A).
- **Domain-adapted model / fine-tuning** (§14.6) — M3, opt-in, out of scope for this build.
