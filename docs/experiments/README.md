# Recorded experiments

Runs kept for autopsy, per SPEC §17.2. Each entry is a `results-*.json` (the raw
per-run records the harness wrote), a `.md` (the autopsy that was read), and the
harness that produced them, so a later reader can re-run the same matrix rather
than take the summary on trust.

`harness.py` drives the matrix; `autopsy.py` reads it. Both keep two questions
apart on purpose:

- **Did the feature work?** — read from real state (a cached job record, a
  persisted note, a `budget_block` event), never from the model's prose.
- **Did the model reach for it?** — the same check read the other way.

A feature can be correct and still never be exercised by a model that does not
call the tool. Conflating those would let a weak model look like a broken
feature, and it is the mistake that makes local-model evaluation useless.

## Index

- **2026-07-27** — waves 0–4 against local ollama models (below)
- **[2026-07-31 — the eFPGA demo: an FPGA from thin air](2026-07-31-efpga-demo.md)**
  — FABulous fabric → model-authored LFSR → routed bitstream → rung E closed.
  Two agentic runs: one capped a single step short with the evidence bundle
  already signed, one completed. Found the `efpga-fabulous` class was
  unreachable from `classify()` and that the install had been dead since the
  autocode rename. Records: `2026-07-31-efpga-demo.json`.
- **[2026-07-31 — eFPGA triage: program or chip?](2026-07-31-efpga-triage.md)**
  — a source-level switch-matrix fault, diagnosed under full blinding. Ten
  runs; seven failed because of a platform, harness, or grader wall, and each
  wall became a committed fix (job.list, fs defaults, input hashes, the
  mechanism-word grader). Records: `2026-07-31-efpga-triage.json`.
- **[2026-07-31 — eFPGA grow (E1): grow the chip](2026-07-31-efpga-grow.md)**
  — a 512-flop requirement overflows 688 BELs; the fix is +8 rows in
  `fabric.csv`. Scripted green first try; agentic best 5/7 — every judgment
  axis demonstrated, the open wall is structured-CSV editing.
  Records: `2026-07-31-efpga-grow.json`.
- **[2026-07-31 — the waveform demo, scripted and agentic](2026-07-31-waveform-demo.md)**
  — one bug driven two ways; seven agentic runs, five eliminated causes, and
  the vacuous-success trap (`gates green` on an empty change set) caught in
  the demo's own verdict logic. Records: `2026-07-31-waveform-demo.json`.

## 2026-07-27 — waves 0–4 against local ollama models

Six scenarios (job cache, tool disclosure, detachable jobs, notebook,
`agent.spawn`, budget ceiling) × five models, one fresh workspace copy per cell
so no run inherits another's cache, history or notebook. `laguna-s-2.1` was
excluded deliberately: 75 GB is too slow to matrix.

**Result: five of six features confirmed firing against a live model**, most on
two independent models. The sixth (job cache) was reached by no model — every
one of them skipped the "run it a second time" instruction — and was verified
deterministically instead, which is how the cache-key bug below was found.

| feature | confirmed by |
|---|---|
| tool disclosure | `gpt-oss:20b` (full round trip → real yosys + OpenSTA jobs → gates accepted), `nemotron-3-nano:4b` (loaded, no follow-through) |
| detachable jobs | `nemotron-3-nano:4b` (+ session parked, + collected by `chipchamp wake`), `devstral:24b` |
| project notebook | `gpt-oss:20b`, `nemotron-3-nano:4b` |
| budget ceiling | `gpt-oss:20b`, `nemotron-3-nano:4b` |
| `agent.spawn` | `devstral:24b` (2 subagents × 10 steps, real findings) |
| job cache | deterministic demo: 0.44s → 0.11s on the same record, correct miss on a new seed and on an edited input |

The single most informative run is `gpt-oss:20b` on `disclosure`: from a
one-line menu entry (`[synth] sta.paths, sta.run, synth.run`) the model called
`tools.load(['synth'])`, received working schemas, drove three real syntheses
and a real STA, and had `report.done` accepted. That is the 72% per-request
context saving demonstrated to cost no capability, rather than asserted.

### What the run was actually worth

Three real defects, none visible to the unit suite:

1. **The job cache never hit for `sim`.** `cache_key` content-hashed every file
   `argv` named — including the job's own *outputs* (`iverilog -o …tb.vvp`),
   which the job rewrites on every run. The key therefore never stabilised. It
   passed every unit test because the test plans declared no artifacts, so the
   output set was empty. Fixed by excluding `plan.artifacts` from the hash;
   `sim` now reuses on the second identical run (0.44s → 0.11s, same job id)
   and still misses correctly on a new seed or an edited input.

2. **`agent.spawn`'s `role` had no `enum`.** A model passed
   `role="design"`/`"verification"` — the *labels* from the request — and was
   rejected. The role list existed only in the error message, which is no help
   to a model that never retries. The names are now in the schema, filled at
   the catalog seam (resolving them inside `meta_tools` is an import cycle that
   fails quietly and yields `enum: []`, which forbids every value). Afterwards
   `devstral:24b` used `role="reviewer"` with `label="design"` — exactly the
   distinction two earlier models had got wrong.

3. **A missing `role` crashed the error path.** `gpt-oss:20b` invented its own
   task shape (`{tool, args}`) with no `role` at all; `t.get("role")` returned
   `None`, `None` entered the bad-role set, and `", ".join(...)` raised a bare
   `TypeError`. The model was told *"sequence item 0: expected str instance"* —
   nothing actionable — and retried the same broken shape twice. The unit test
   passed the string `"wizard"`, so it never touched the `None` path. Missing
   roles and empty prompts are now named, with the catalogue and a worked
   example attached.

### `qwen3-coder:30b` — unresolved, and an explanation I got wrong

Three scenarios, three timeouts at 411–414s with **zero steps** and no tool
call. My first explanation was that this box has no usable GPU offload (true:
no `nvidia-smi`, and `llama-server` burns 370–405% CPU) and that dense models
≥20B are therefore too slow.

**That explanation is wrong.** `devstral:24b` — genuinely dense, 24B — runs
fine through the same path at ~39s/step, and `gpt-oss:20b` at ~13s/step.
Meanwhile `qwen3-coder:30b` is an **MoE with ~3B active parameters**, so by
compute it should be the *fastest* of the three, not the only one incapable of
a single step.

So it is not a speed problem. What it is, I could not establish: a native
non-streaming probe returned a valid tool call reporting ~2s of compute but
223s `total_duration`, and every attempt to isolate the endpoint (native vs
OpenAI-compat, with and without tools, warm vs cold) exceeded a 200–600s
budget — including a plain warm. Two circumstantial details worth keeping for
whoever picks this up: ollama runs it with `--no-jinja --chat-template chatml`,
which would not match this model's own tool-call template; and the failure is
specific to one model across three different scenarios.

Recorded as **unresolved**. A model-specific incompatibility and "too slow for
interactive use" call for different responses — the first is something the
router should route around — so guessing between them would be worse than
saying it is open.

`qwen3.6:35b` is a separate, cleaner fact: **HTTP 500 at load**, 23 GB, almost
certainly OOM on this machine.

### One lab artifact, called out so it is not read as a model fact

An orphaned `qwen3-coder` generation — left by a `pkill -9` of my own during
harness development — pinned the accelerator for over an hour and blocked
`devstral` and `gpt-oss` from loading. Their first-pass `unavailable` rows were
that, not the models; both were re-run once the orphan cleared and their real
results supersede the placeholders here.
