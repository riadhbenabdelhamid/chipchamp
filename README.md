# Chipchamp

**An agentic coding platform for RTL design & verification** — Claude Code / Codex for
digital hardware. Chipchamp writes, verifies, debugs and refactors SystemVerilog
together with its verification collateral, and drives a real EDA toolchain
(simulation, lint, synthesis, formal, logic-equivalence) as first-class agent tools.

This repository is a working **M0 implementation** of [`SPEC.md`](SPEC.md): the
deterministic *platform substrate* the spec argues is the actual product — the
loop around the model — plus a model-agnostic agent core that drives it.

> **Thesis.** General-purpose coding agents underperform on RTL not because LLMs
> write bad Verilog, but because *the loop around the model is wrong for hardware*:
> feedback is slow and licensed, ground truth lives in gigabyte waveforms and
> coverage databases, and a wrong answer costs a silicon respin. Chipchamp rebuilds
> the loop around those constraints. Trust is earned by **machine-generated
> evidence**, not model prose.

---

## What actually works here

Everything below runs against a **real** open-source EDA toolchain (Verilator,
Icarus, Yosys, Verible, SymbiYosys, eqy, GHDL) — no mocks:

| Layer | Status | SPEC |
|---|---|---|
| **Design database** — **slang** production SV front end (pyslang; pragmatic parser fallback) → elaborated hierarchy w/ resolved params, clock/reset domains, FSM extraction, fan-in cone slicing, semantic diff, persistence + staleness. Shakes out on a real RISC-V core (**ibex: 176 modules in ~6 s, 0 warnings**) | ✅ | §8.3 |
| **EDA adapter layer** — normalized diagnostics + capability manifests over 7 real tools; graceful degradation | ✅ | §8.4 |
| **Job orchestration** — reproducible job records, license-token pools, regression manager with failure-signature clustering + flake detection | ✅ | §8.5 |
| **Waveform service** — VCD parser + indexed query API (`value`/`when`/`changes`/`compare`/`trace_x`) + ASCII/WaveJSON render | ✅ | §8.6 |
| **Coverage service** — Verilator/functional/**UCIS XML** (Questa/VCS/Xcelium export)/**cocotb-coverage** ingest (format-sniffing `cov.load`), holes, attribution, anti-gaming baseline gate — functional coverage live on open tools | ✅ | §8.7 |
| **Policy engine** — task classification, verification ladder V0–V4, gates, autonomy L0–L3, budgets, context ACLs, anti-gaming detectors | ✅ | §11 |
| **Evidence bundles** — platform-signed, tamper-evident, assembled from job records | ✅ | §8.8, §11.3 |
| **Agent tool catalog** — 91 tools with JSON schemas, cost classes, permission tiers, runtime capability discovery (`caps`) | ✅ | §9 |
| **Model layer** — provider-agnostic (Anthropic · OpenAI · local: LM Studio/Ollama/vLLM/llama.cpp) with live model discovery, a `/model` selector, and a context audit log | ✅ | §14, §13.3 |
| **Agent loop + CLI** — model-driven loop, scripted playbooks (P1 triage, P6 lint), headless + REPL | ✅ | §7.1, §8.2 |

A gap-closure pass then implemented most of what the spec described beyond M0 —
multi-agent orchestration, MCP (server + client), STA + timing gate, a register-map
compiler with a source-of-truth map, waivers-as-data, FuseSoC/Bender ingestion,
FST, cocotb, prompt-injection flagging, `mr.prepare`, model routing, an APB
protocol pack with a live formal proof, ChipchampBench, a red-team suite, a CI gate,
and a static dashboard. See [`docs/GAPS.md`](docs/GAPS.md) for the full closure
matrix (what's live vs. fixture-validated vs. dry-run).

A later pass added a **physical vertical — RTL-to-GDSII** (beyond the spec's
original NG4), driving the full **LibreLane** flow (OpenROAD · Yosys · Magic ·
KLayout · Netgen) on the open **sky130** PDK: synthesis → floorplan → placement →
CTS → routing → DRC/LVS/antenna signoff → GDS. The `librelane` adapter reads
LibreLane's normalized `metrics.json` (~300 signoff metrics — "query, don't dump"
at the layout scale) into a `physical` task class with **signoff gates**
(DRC-clean / LVS-clean / timing-met across corners / antenna-clean). Verified live
here: the example `counter` taped out clean on sky130 (setup WNS +1.64 ns, DRC 0,
LVS 0, antenna 0, 775 µm² of cells) → a 213 KB GDSII. `chipchamp pnr <module>` runs
it; `pd.metrics` / `pd.drc` query the results. Signoff and tapeout stay the
human's call (NG2/NG9) — the platform reports verdicts + evidence, it doesn't
authorize a tapeout.

And an **eFPGA-FABulous vertical** via **FABulous**: generate an embedded-FPGA
fabric, map a user RTL design onto it (Yosys synthesis → `nextpnr-generic
--uarch fabulous` place & route → `bit_gen` bitstream), and optionally harden
the fabric to a GDSII macro (which reuses the physical vertical). The `fabulous`
adapter normalizes the flow into an `efpga-fabulous` task class with gates
(`fabric_generated`, `bitstream_generated` + routed). Verified live here: the
demo `sequential_16bit_en` design mapped onto a generated fabric → a **12 KB
bitstream**, routed clean. `chipchamp efpga-fabulous fabric` / `chipchamp
efpga-fabulous bitstream <design>` run it; `efpga-fabulous.info` queries results.

A **DV-methodology pass** made UVM and functional coverage first-class:

- **UVM, license-free** — `chipchamp uvm <module>` scaffolds a complete runnable
  bench from the design database (interface, seq_item, driver, monitor, agent,
  env, scoreboard stub, sequences, base+smoke test, tb top, filelist + a
  manifest entry that preserves your `tests.yaml` comments). It compiles against
  the real **IEEE 1800.2 Accellera uvm-core** under **Verilator 5** (`--timing`,
  `-j 0`) — and unchanged on Questa/VCS (`-ntb_opts uvm`). The simulator-neutral
  **UVM report parser** reads the report server's own format from any simulator
  log and enforces the rule that matters: **UVM_ERROR/UVM_FATAL > 0 (or a
  missing report summary) fails the job even when the process exits 0** — the
  verdict lands in job records, so gates and `report.done` see it.
- **Functional coverage, stack-neutral (UCIS)** — `cov.load` sniffs and ingests
  **UCIS 1.0 XML interchange** (what Questa `vcover`, VCS/urg and Xcelium/IMC
  export), **cocotb-coverage XML**, Verilator `.dat`, or native JSON into one
  normalized model, so `cov.holes` / attribution / the **baseline anti-gaming
  gate** work identically over a licensed UCDB export or a cocotb run. Proven
  live license-free: a cocotb-coverage TB over Icarus exports covergroups →
  `cov.load` → `cov.holes` surfaces the deliberately-unhit `(en=0,clr=1)` bin.

And a **PPA feedback loop (physical → RTL)** on top of the GDSII vertical —
closing the loop the other way:

- **`pd.critical`** — parses OpenROAD's post-route per-corner path reports and
  **cross-probes each start/endpoint back to RTL**: anonymized netlist flops
  (`_54_`) recover their RTL nets (`count[7]`) from the synthesis netlist
  (instance names are stable through PnR), resolve through the design DB to
  module + `file:line`, and arrive with the signal's fan-in/fan-out **cone** and
  the worst per-stage delays. The example counter's worst ss-corner path lands
  on `assign tc = en & (count_q == …)` at `counter.sv:27` — with the three
  heaviest gate stages named.
- **`pd.area`** — per-module area attribution in seconds (hierarchy-preserving
  yosys synth mapped to the ciel-managed sky130 liberty): instances ×
  per-instance µm², parameterized modules demangled, sorted by design area. The
  example SoC prices out at 19,570 µm² with the FIFO as the 9.3 kµm² hog.
- **`pd.ppa`** — one PPA picture per module from the pnr job's ~300 metrics
  (+ optional per-module area breakdown), with `save_baseline` → later calls
  report **signed deltas annotated better/worse** (slack up = better, area/power
  up = worse). Edit RTL → `pd.run` → `pd.ppa` answers "what did that cost."

A **PPA depth pass** then made the loop an optimizer, not just a reporter:

- **`pd.power` — activity-driven power.** The vectorless watt in `metrics.json`
  is a guess (default toggle rates). `pd.power` annotates the **routed**
  netlist with a real workload VCD (the waves a sim job already dumped), runs
  the devshell's own OpenSTA (`read_power_activities` → `report_power`), and
  reports power by group — the **clock-tree share** is the clock-gating signal
  (28.3% on the example counter, with the three clock buffers as the top
  consumers) — plus the hottest instances cross-probed to RTL and the
  **annotation rate**, so a bad VCD can't pose as a trustworthy number.
  Baselines → signed deltas, and vectorless results carry an explicit warning.
- **`pd.area --by-line` — µm² per RTL line.** Flops keep exact yosys `src`
  refs through liberty mapping; ABC-stripped combinational cells are attributed
  to the nearest source-carrying cell downstream (their endpoint register's
  line), with the remainder reported honestly. The example SoC resolves to
  `sync_fifo.sv:30` = 8,220 µm² / 311 cells — the memory array line.
- **`pd.deadwidth` — coverage × area.** Per-bit toggle coverage (which bits
  never moved across the workload) crossed with the mapped netlist + liberty
  (what each bit's flop costs): oversized counters/buses/state vectors that no
  single tool sees. Live on the example: `wr_data[8:31]` never toggles in a
  32-bit FIFO port.
- **`pd.sweep` — strategy Pareto.** One yosys run per ABC mapping script +
  one real OpenSTA delay per mapped netlist (measured >50% area swing on
  comb-heavy logic), or full LibreLane `SYNTH_STRATEGY` runs in pnr mode —
  dominated configs marked. On the example FIFO the "delay" recipe is
  dominated: the area script is both smaller *and* faster.
- **`chipchamp optimize` (P11)** — the scripted brief chaining all of the
  above into a ranked worklist where every item names its proof loop
  (`fs.edit → lec.run → pd.run → pd.ppa/pd.power`): optimizations arrive
  **LEC-proven and measured**, or they don't count. Missing evidence (no
  toggle coverage, no routed design) becomes measurement work items, never
  silence.
- **`pd.glsim` — gate-level activity, 100% annotation.** Simulates the
  **routed netlist** against the existing testbench using the PDK's functional
  cell models (zero-delay). Two deliverables in one job: the taped-out netlist
  **passes its RTL self-checks**, and its VCD covers every gate net — so
  `pd.power` reaches **100% pin annotation** (181/181 on the example) instead
  of boundary-annotation + propagation, which measured **79% higher** than the
  gate-accurate number on the same workload.
- **The loop, closed and regression-tested.** `tests/test_optimize_loop.py`
  runs the whole worklist-item lifecycle live in ~5 s: toggle coverage flags a
  32-bit register that never exceeds 9 → `pd.deadwidth` prices 28 dead flops →
  the narrowing edit is applied → **eqy proves equivalence** (state
  re-encoding defeats k-induction; the adapter's added **PDR strategy**
  discovers the reachability invariant) → `pd.area` measures >400 µm²
  recovered → `report.done` **accepts** on real job records (lint, smoke sim,
  LEC, no-gaming). The same task was also driven by a **local model**
  (Ollama qwen3.5:9b) through the agent loop: plan → `pd.deadwidth` → recover
  from missing coverage → `cov.run` → re-check → report.

The same loop extends to **FPGA design checkpoints** (`chipchamp fpga <module>`):

- **Two flows, one checkpoint discipline** — the open flow (yosys →
  `nextpnr-<family>`: ice40/ecp5/machxo2/nexus) writes `post_synth` /
  `post_route` JSON checkpoints + a machine-readable report; **Vivado** (batch,
  non-project) writes a **real `.dcp` at every stage** — `post_synth` →
  `post_place` → `post_route` — each with timing/utilization (+ power and worst
  paths post-route). Both proven live here: counter → iCE40 in ~2 s (fmax 365
  vs 100 MHz target), counter → `xc7a35t` under Vivado 2025.1 in ~2 min (WNS
  +2.6 ns @ 200 MHz, 9 LUT / 8 FF, 3 DCPs on disk).
- **`fpga.critical`** — worst path in RTL terms. The open flow is the best case
  of all: nextpnr's report carries **native RTL source refs per path segment**
  (yosys `src` attributes survive P&R), so segments arrive already labeled
  `counter.sv:13`. Vivado paths resolve `count_q_reg[3]/C` → `count_q` through
  the design DB to module + `file:line` + fan-in cone, with the logic/route
  split and LUT levels.
- **`fpga.ppa` / `fpga.checkpoints`** — normalized per-stage snapshots
  (fmax/WNS, LUT/FF/BRAM/DSP, power), stage-to-stage comparison (how much did
  routing eat?), and saved baselines → signed better/worse deltas per RTL edit.
- **`fpga.bitstream`** — the flow reaches a **flashable artifact**, not just a
  routed netlist: `icepack` / `ecppack` / `prjoxide` by family, or Vivado
  `write_bitstream`. P&R always writes the packer's input, so packing never
  re-runs the flow. Both paths proven live here: `chipchamp fpga counter
  --bitstream` → a real 135 100-byte iCE40 `.bin`, and a 2 192 131-byte
  `xc7a35t` `.bit` under Vivado 2025.1. A Vivado bitstream needs a full
  (`ooc=false`) run **and** pin constraints (`fpga.run xdc=<file>`) — an
  out-of-context job is refused up front instead of burning a doomed run, and
  unconstrained ports are reported as exactly that (Vivado's UCIO-1/NSTD-1)
  rather than "see the log". chipchamp won't downgrade those DRCs for you: a
  bitstream with arbitrary pin placement can damage hardware.
- **`fpga.pins` / `chipchamp pins`** — pin problems caught in a second instead
  of after a multi-minute run: a typo'd port, an unconstrained one, a missing
  IOSTANDARD, two ports on the same pin. It validates `.xdc`/`.pcf`/`.lpf`
  against the design DB's *real* bus-expanded ports and exits non-zero for CI.
  It also **generates** constraints for a **board** — 28 Digilent boards are
  read straight out of Vivado's installed board files, and `--import` turns any
  vendor master constraints file into a profile. Board data is authoritative
  only: an unmapped port becomes a `# TODO`, never a guessed pin, because a
  wrong pin can short an output. Constraints files are Tcl, so `foreach` and
  wildcards are reported as *unparsed and unchecked* rather than mis-read.
- **Policy** — an `fpga` task class, rung **F** = `fpga_routed` + `fpga_fits` +
  `fpga_timing_met` + `fpga_bitstream`. `fpga_fits` is the FPGA-defining
  constraint: per-resource used-vs-available from the routed stage, reporting
  the tightest resource; it defaults to "must fit" (never a false failure) and
  `[fpga] max_utilization = 0.85` demands headroom. `.xdc`/`.pcf`/`.lpf`
  constraint edits classify as `fpga` so they force re-implementation. The
  eFPGA-FABulous vertical feeds the same shape: FABulous runs report fmax from the
  embedded nextpnr log.

The same discipline extends to a **RISC-V vertical** (`chipchamp riscv …`) — a
core is only a RISC-V core if it executes the ISA the way the ISA says, and that
is checkable mechanically:

- **`riscv.compile`** — C/assembly → ELF + a `$readmemh` memory image (word
  width configurable, little-endian) + disassembly + symbol table, with an
  explicit `-march`/`-mabi` because the ISA string is the *contract under test*.
  Verified live on `riscv64-unknown-elf-gcc 15.2.0`.
- **`riscv.refrun`** — runs the same ELF on a reference model: **spike** when
  installed, otherwise the gdb/binutils simulator that ships *with the
  toolchain*, both normalized to one trace shape. Bounded by step count **and**
  wall clock, because bare-metal test programs conventionally end in a spin
  loop.
- **`riscv.cosim`** — the payoff. Compares the core's retired-instruction
  stream against the reference's and reports the **first divergence**:

  ```
  ✗ divergence at instruction #2: the core executed PC 0x00001040
    but the reference executed 0x00001008 (add gp, ra, sp)
      0x00001000  addi ra, zero, 0x5
      0x00001004  addi sp, zero, 0x7
  ```

  That is the difference between "test 7 fails" and a named instruction. With
  spike installed it compares **register writebacks**, not just PCs. Core traces
  are parsed permissively (any line carrying `pc=…`), so a team needs no format
  change; the model's bootrom is skipped **automatically** (spike runs five
  instructions before reaching your entry point); and agreement over a *prefix*
  is reported as a prefix, never as proof of full equivalence.
- **Barrel-multithreaded cores** — a barrel core interleaves T threads into one
  retired stream, so comparing it whole against a single-hart model diverges
  immediately. cosim reads a `hart` from the trace (spike's `core N:`, or your
  testbench's `tid=`/`thread=`), **demultiplexes**, compares each thread against
  the model separately, and names the faulty thread rather than blending the
  verdict. Pick the model with `model=spike|gdbsim`: spike's debug module owns
  `0x0–0x1000`, so a core booting at 0x0 (4 KB, no DRAM at 0x8000_0000) uses the
  gdb simulator with `region="0x0,0x1000"`.
- **`riscv.compliance`** — the conformance signoff: drives **RISCOF** over
  `riscv-arch-test`, running every architectural test on the core and on the
  reference model and comparing **signatures**. RISCOF is driven as an external
  tool (it pins `pyyaml==5.2`, so it belongs in its own venv, never the host's).
- **Policy** — a workspace that declares `[riscv]` classifies RTL edits as the
  `riscv` task class, rung **R** = the V3 functional gates **plus** `isa_cosim`
  and `isa_compliant`. Choosing it never trades away ordinary RTL rigor, and
  both ISA gates read machine verdicts, not model prose.

**Agent Skills (§15.3)** — user-authored expert playbooks in the standard
`SKILL.md` format (YAML frontmatter `name` + `description` with "Use when…"
trigger prose, markdown body, optional bundled `references/` files):

- **Point at your library**: `chipchamp skills add ~/skills` — the registry is
  per-workspace and persists; `skills list` / `skills remove <dir>` /
  `skills remove --all` / `skills show <name>` manage it. Layered discovery
  with first-wins precedence: `.chipchamp/skills/` (workspace) →
  `[skills] dirs` in config.toml (versioned team intent) → registered dirs.
  Tolerant frontmatter parsing (real-world descriptions with unquoted `:` load
  fine); malformed skills are skipped with a warning, never a crash.
- **The agent uses them itself**: by default (`[skills] index = "semantic"`)
  each skill's "Use when…" prose is embedded once via a local
  `/v1/embeddings` endpoint (LM Studio/Ollama/vLLM auto-detected; cached in
  `.chipchamp/skills.vec.json`, prebuild with `chipchamp skills reindex`) and
  only the `top_k` skills relevant to the task ride in the system prompt —
  O(top_k) tokens instead of O(corpus), with an explicit skill-name mention
  always winning over similarity, and automatic fallback to the static index
  when no embedding endpoint is reachable. `index = "compact"` restores the
  full name+gloss list on every call (`"full"`/`"off"` as before). When a
  skill matches, the model calls `skill.use(name)` for the full playbook and
  `skill.read(name, path)` for bundled references — path-confined to the skill
  directory, and scanned by the FR-SEC-05 injection detector like any other
  untrusted content. Skill text is guidance, never policy.
- **Or invoke manually**: `/skill <name>` (or directly `/<skill-name>`,
  tab-completed) stages the playbook as context for your next message;
  built-in commands always win name collisions.

**IP library (reuse before rewrite)** — point Chipchamp at a component library
(a directory with a `manifest.json` in the riscvier convention: per-module
category/tags/params/clocks/resets/ports + verification status):

- **Register**: `chipchamp library add ~/rtllib-pillars` (or `[library] dirs` in
  config.toml); `library list/show/search/match/fetch` manage and query it. A
  compact index (library + category counts) rides in the system prompt.
- **Match by function + interface, not names** (`lib.match`) — when the agent
  decomposes an architecture it names sub-blocks in its own vocabulary that
  rarely matches a component's name lexically. `lib.match(behavior, interface)`
  embeds each component's `summary+description+tags+interface-signature` once
  (cached in `.chipchamp/library.vec.json`; `library reindex` prebuilds) via the
  same local `/v1/embeddings` endpoint as skills, and ranks by **semantic
  similarity + structured interface compatibility** (protocol / valid-ready
  handshake / clock-domain count from the manifest's `interfaces`/port-`role`s).
  So "an elastic buffer with backpressure, valid/ready, single clock" finds
  `fifo_sync`/`stream_register` with no keyword overlap; falls back to lexical +
  interface scoring with no endpoint.
- **The agent reuses instead of rewriting**: `lib.match`/`lib.search` →
  `lib.info(name)` (full card + a ready-to-edit instantiation template) →
  `lib.fetch(name)` copies the component's RTL fileset (parsed from its FuseSoC
  `.core`, else by convention) into `rtl/lib/` through the policy gate — every
  fetched file is a recorded edit, visible to gating like any other change.

Still open by design (spec roadmap §18 / external constraints): FSDB reading
(needs Verdi), live validation of the commercial-tool adapters, signoff CDC,
SoC-scale performance numbers, and SOC 2 — see the bottom of `docs/GAPS.md`.

---

## Quickstart

```bash
# 1. one-shot bootstrap: creates .venv and installs chipchamp (PEP 668-safe)
./setup.sh                      # or:  make setup
source .venv/bin/activate       # activate the venv in each new shell
export PATH="$HOME/oss-cad-suite/bin:$HOME/verible/bin:$PATH"   # your EDA tools

# 2. start the interactive session on the bundled example SoC (like claude/codex)
chipchamp --root examples/soc
#   → drops into a REPL. Type a task in plain English, or use slash commands:
#       » /hier soc_top          elaborated hierarchy w/ resolved params
#       » /sim fifo_smoke        compile + run, dump waves
#       » /triage                run tests, cluster failures (P1)
#       » why does fifo_smoke fail on seed 3?   ← plain English → the agent
#       » /model                 pick a provider/model (Anthropic/OpenAI/local)
#       » /help                  all commands · /exit to quit

# every command also works headless (scripting / CI):
chipchamp --root examples/soc adapters          # which EDA tools are available
chipchamp --root examples/soc index             # build the design database
chipchamp --root examples/soc lint              # normalized lint diagnostics
chipchamp --root examples/soc sim fifo_smoke    # compile + run, dump waves
chipchamp --root examples/soc -p "find the root cause of the fifo mismatch"

# 3. see the whole loop end-to-end (inject a bug → triage → fix → gates → evidence)
python demo.py

# 4. the unattended side — built for cron, not for someone watching a REPL
chipchamp --root examples/soc nightly           # regress → flake history →
                                                #   per-cluster triage → dashboard
chipchamp --root examples/soc wake              # collect a parked session's
                                                #   detached jobs and continue
chipchamp --root examples/soc flaky             # tests whose own history
                                                #   contradicts itself
chipchamp --root examples/soc notebook          # what this project has learned
chipchamp --root examples/soc experiment autopsy  # compare recorded runs
```

Inside a session, `/undo` reverts the files the last task changed (anything you
edited by hand since is kept), and the gate ladder repaints under the
conversation as jobs land — so you can see which rung the work is on rather
than finding out at `report.done`.

**Launch from any repo.** Run `chipchamp` anywhere and the repo you're in becomes
the working repo — it walks up to the `.chipchamp/` project root, else the git
top-level, else the current directory. Override with `--root <path>`,
`CHIPCHAMP_ROOT`, or pin a default that works from anywhere:

```bash
cd ~/work/my-soc && chipchamp          # my-soc is the working repo (auto-detected)
chipchamp workspace ~/work/my-soc      # pin it as the default (used from anywhere)
chipchamp workspace                    # show the resolved repo + how it was chosen
chipchamp --root ~/work/other sim …    # one-off override (beats the pin)
```

Precedence: `--root` > `CHIPCHAMP_ROOT` > pinned default > auto-detected repo root.
With no `.chipchamp/config.toml`, Chipchamp infers a target named after the repo by
scanning conventional RTL dirs (`rtl/`, `hdl/`, `src/`…) **plus a `work/` scratch
dir**, globbed *live* so a design the agent writes this session is immediately
simulable — and never sweeping bundled `examples/`, vendored, or fixture trees
(which would drag uncompilable collateral into every sim/synth). `hier`/`lint`/
`sim` work on a fresh repo with zero config.

**Where the agent works.** New designs, generated testbenches and scratch go
under `<root>/work/` (override with `[project] work_dir`), created on session
start — the agent is told to write there and not scatter files across the repo
or edit unrelated directories.

**Autonomy modes (shift-tab).** Like Claude Code, three modes cycle with
**shift-tab** (or `/mode [name]`), shown in the bottom toolbar: **normal**
(edits & jobs run; destructive actions confirmed) · **auto** (full autonomy;
destructive still confirmed) · **plan** (approve every edit & job). **Destructive
actions always require an explicit typed confirmation in every mode** — `fs.delete`
and any tool marked destructive can never be auto-approved, and decline off a TTY.

**Resumable sessions.** `chipchamp --continue` resumes the most recent session in
the workspace, `chipchamp --resume <id>` a specific one; in-session `/sessions`
lists them (newest-first, with a preview) and `/sessions resume <id>` swaps the
live transcript. **Live job visibility**: a running EDA job (sim/synth/pnr/…)
shows a `running <tool> · Ns` spinner, then an indexed `▪ J-0042 sim.run passed`
line colored by verdict.

**Two surfaces, one tool.** Bare `chipchamp` is an interactive agent session
(plain-English tasks go to the model; `/command` runs a skill). Every `/command`
is also a shell subcommand (`chipchamp sim …`, `chipchamp ci …`) for scripting and
CI, and `chipchamp -p "…"` is a headless one-shot.

The session is a proper terminal app (like Codex/Claude Code): it clears the
screen on entry, shows a **live command menu when you type `/`**, renders agent
replies as **Markdown** with **syntax-highlighted SystemVerilog**, shows edits as
**colored diffs**, displays an **elapsed-time "thinking" spinner** while the model
generates, and shows a **live plan checklist** the agent keeps current as it works
(via the `plan.update` tool). This needs `prompt_toolkit` (installed by `setup.sh`
via the `tui` extra); without it, input falls back to readline tab-completion and
the rest still renders.

**Plan mode.** Run `chipchamp --plan` (or `/plan` in-session; on by default when a
path's autonomy is L0) and the agent must **get your approval before every edit or
job** — you see the action (`fs.write rtl/…`, `sim.run …`) and answer
`y`/`a`(ll)/`N`. Declining returns a "not approved" result so the agent replans
rather than proceeding. Off a TTY, plan mode declines automatically (a safe
preview). This pairs with the autonomy ladder: signoff stays with the human.

Every model call has a wall-clock timeout (default 600 s; per-model via
`/timeout`, or `[model] request_timeout` / `CHIPCHAMP_MODEL_TIMEOUT`). While a
response streams it bounds the gap between tokens, not the whole turn, so a slow
model that keeps generating isn't cut off; a wedged one still times out with a
clear message (and an offer to retry with a bigger budget). See **Slow models**
below.

### The headline demo

`python demo.py` injects a real bug into the FIFO (`rd_data` read from the write
pointer), then drives the platform through the whole loop on genuine tools:

```
3. Triage: the smoke test now fails
   J-0021: fail — fifo data mismatch @220000 exp=a001 got=a000
4. Query, don't dump: compare failing run vs golden; walk the cone
   first divergence: tb_sync_fifo.dut.rd_data @ 45000 (fail=x vs golden=1010…)
   cone: assign rd_data = mem[wptr];
   ROOT CAUSE: rd_data is driven from mem[wptr] instead of mem[rptr]…
6. Re-verify:  0 lint errors · re-sim pass · coverage non-regressing
7. report.done accepted: True
   ✅ lint  ✅ smoke_sim  ✅ affected_regress  ✅ coverage_baseline  ✅ no_gaming
8. Signed evidence bundle — signature sha256:845bc27a…  verify: True  gates: True
```

Nothing here is narrated by a model: every ✅ is validated against a real job record.

---

## How it embodies the spec's principles

- **Evidence over eloquence.** `report.done` is the *only* way to declare success, and
  it validates every required gate against signed job records — the agent is
  structurally unable to claim done otherwise ([`policy/gates.py`](chipchamp/policy/gates.py)).
  Evidence bundles are signed and tamper-evident ([`evidence/bundle.py`](chipchamp/evidence/bundle.py)).
- **Query, don't dump.** Waveforms/coverage/logs are exposed as bounded queries
  (`wave.when`, `wave.compare`, `cov.holes`, `job.log` grep) — no raw dump ever
  enters context ([`waves/store.py`](chipchamp/waves/store.py)).
- **Cheap loops inside, expensive outside.** The verification ladder V0–V4 is
  policy-bound to task classes ([`policy/gates.py`](chipchamp/policy/gates.py)).
- **The human owns signoff.** Autonomy is a per-path × task-class dial that never
  reaches auto-merge for functional changes; waivers/exclusions are approval-gated
  *data*, not edits ([`policy/autonomy.py`](chipchamp/policy/autonomy.py)).
- **IP is radioactive.** Context ACLs are enforced *below the model* — a denied
  PDK path never reaches it ([`policy/acl.py`](chipchamp/policy/acl.py)); every model
  call is recorded in a context audit log ([`agent/gateway.py`](chipchamp/agent/gateway.py)).
- **No gaming.** Green-by-deletion, disabled assertions, shrunk timeouts, narrowed
  randomization, pinned seeds and covergroup edits are detected and block completion
  ([`policy/antigaming.py`](chipchamp/policy/antigaming.py)).
- **Reproducible.** Every job records its tool version, env, inputs hash and seed;
  `chipchamp repro <job>` replays it ([`jobs/record.py`](chipchamp/jobs/record.py)).

## Architecture

```
CLI  ── Workspace(config.toml, tests.yaml, policy.yaml, DESIGN.md)
 │        │
 │        ├── DesignDB      (design/)    parser → hierarchy, domains, FSM, cone
 │        ├── AdapterRegistry (adapters/) verilator·icarus·verible·yosys·eqy·sby·ghdl
 │        ├── JobRunner     (jobs/)      records · repro · license pools · regression
 │        ├── WaveStore     (waves/)     VCD → value/when/compare/trace_x + render
 │        ├── CoverageSvc   (coverage/)  ingest · holes · attribution · baseline gate
 │        └── PolicyEngine  (policy/)    classify · gates · autonomy · ACL · anti-gaming
 │
 ├── ToolContext (tools/)   43 tools: design.* sim.* wave.* cov.* lint/synth/lec/formal · report.done
 └── AgentLoop (agent/)     model gateway (+ audit log) · scripted playbooks · session store
                           → EvidenceBundle (evidence/)  signed, from job records
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full code→spec map.

## Tests

```bash
make test               # or: pytest -q  (inside the venv)
```

## Troubleshooting

**`error: externally-managed-environment` (PEP 668)** — you ran `pip` against the
system Python. Chipchamp installs into a local venv instead: run `./setup.sh` (or
`make setup`), then `source .venv/bin/activate`. Every `make` target and the
`chipchamp` command run inside `.venv`. You can also invoke it without activating:
`.venv/bin/chipchamp …` or `.venv/bin/python -m chipchamp …`.

## Layout

```
chipchamp/          the package (design, adapters, jobs, waves, coverage, policy, evidence, tools, agent, cli)
examples/soc/      a small multi-clock SoC: counter, sync FIFO, arbiter FSM, top + testbenches + .chipchamp/ config
tests/             pytest suite (real-tool tests skip cleanly if a tool is absent)
demo.py            the end-to-end showcase
SPEC.md            the full product & engineering specification this implements
```

## Choosing a model (any provider)

Chipchamp is model-agnostic (SPEC §14). It speaks to **Anthropic**, **OpenAI**, and
any **OpenAI-compatible** server — including local ones a hardware team runs inside
its own network (**LM Studio**, **Ollama**, **vLLM**, **llama.cpp**), which is what
makes the on-prem / air-gapped deployment modes real.

```bash
chipchamp model                       # discover reachable providers + models, pick one interactively
chipchamp model --list                # just list what's accessible
chipchamp model --set ollama:qwen3.5:9b   # or lmstudio:…, openai:gpt-4o, anthropic:claude-sonnet-5
chipchamp --root examples/soc agent -p "why does fifo_smoke fail?"   # uses the selection
```

Inside the `chipchamp agent` REPL, type `/model` to switch providers/models mid-session.
Selection resolves in order: `.chipchamp/model.json` (written by `/model`) → env
(`CHIPCHAMP_PROVIDER`/`CHIPCHAMP_MODEL`, or `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) →
config `[model]` → none. Custom endpoints go in `[providers.*]` in `config.toml`.
The same agent loop drives every provider — verified live here against a local
**Ollama** model, which called the design tools and `report.done` through the
OpenAI-compatible path.

**Tuning the model.** Beyond `/effort` (reasoning depth), `/model tune` exposes every
generation/runtime parameter your server accepts — sent verbatim in the chat request,
persisted per-model in `.chipchamp/model_opts.json`:

```bash
/model tune num_ctx=8192 temperature=0.4 top_p=0.9   # per selected model
/model tune --global seed=1                           # apply to every model (*)
/model tune num_ctx                                   # clear one key
/model tune                                            # show what's in effect
chipchamp model --tune num_ctx=8192 --tune temperature=0.4   # headless equivalent
```

Values are JSON-typed (`8192`→int, `0.7`→float, `true`→bool); local servers
(LM Studio/Ollama) take `num_ctx`, `num_predict`, `top_k`, `repeat_penalty`, `seed`,
…, while Anthropic honors the sampling subset it supports and ignores the rest.
`/model tune --help` lists the common Ollama / LM Studio parameters.

**Slow models.** A big local model can exceed the default 600s per-call budget.
Rather than one global limit, the budget is **per-model** (`.chipchamp/timeout.json`):

```bash
/timeout 20m            # raise the budget for the selected model
/timeout --global 900   # a default for every model
/timeout off            # no wall-clock limit
chipchamp model --timeout 1200      # headless equivalent
```

Two things make slow models usable: while a response is **streaming**, the budget
bounds the *gap between tokens*, not the whole turn — so a model that keeps
generating never times out; and when a call does time out, the session offers a
one-key **retry with a doubled budget** (or `/router on` to auto-switch to a
faster model). A cold first call also says so, since a large model can take a
while to load.

## Reaching the internet (read-only)

Three agent tools let the loop *read* external information — never download or execute:

- **`web.search`** — keyword search. Keyless **DuckDuckGo** by default; a configured
  `[web] provider = brave|tavily|serper` with `api_key`/`api_key_env` for reliable
  results (keyless search can be rate-limited from datacenter IPs).
- **`web.fetch`** — HTTP(S) GET one URL → readable text (HTML stripped to prose),
  read into memory and size-capped; nothing hits the disk.
- **`research`** — delegates an open question to a bounded **web-researcher**
  sub-agent (search + fetch + read-only repo access) that returns a source-cited
  answer. Available to the orchestrator as the `web-researcher` role too.

## Notes & limitations

- The SV front end is a pragmatic, dependency-free parser targeting the synthesizable
  subset (the spec's production front end is slang). It degrades gracefully on
  preprocessor-heavy code and flags reduced confidence.
- Tool-calling quality varies by model: frontier models and strong local models
  (Qwen3, gpt-oss, etc.) drive the tools well; smaller local models may not call tools.
  Without any model, the platform still runs its scripted playbooks fully offline.
- Commercial EDA, farm connectors, and the IDE/CI/web surfaces are M2+ per the roadmap.

Licensed under AGPL-3.0.
