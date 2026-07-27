# Chipchamp architecture — code → spec map

This maps the implementation to [`SPEC.md`](../SPEC.md). Each module's docstring
cites the spec section and requirement ids it realizes.

## Layer map

| Package | Responsibility | SPEC | Key files |
|---|---|---|---|
| `chipchamp/design/` | Design Intelligence Layer | §8.3 | `lexer.py` (preprocess+tokenize), `parser.py` (modules/ports/params/instances/always/assign), `model.py`, `database.py` (elaboration, xref, persistence, semantic diff), `domains.py`, `fsm.py`, `cone.py`, `params.py` (constant eval) |
| `chipchamp/adapters/` | EDA Adapter Layer | §8.4 | `base.py` (Adapter/Plan/Diagnostic/Manifest), `diagnostics.py`, `verilator.py`, `icarus.py`, `verible.py`, `yosys.py`, `eqy.py`, `symbiyosys.py`, `ghdl.py`, `registry.py` |
| `chipchamp/jobs/` | Job Orchestration | §8.5 | `record.py` (provenance+repro), `runner.py` (local runner, license pools, log grep), `regression.py` (clustering, flake) |
| `chipchamp/waves/` | Waveform Service | §8.6 | `vcd.py` (parse+index), `store.py` (queries), `expr.py` (4-state eval), `render.py` (ASCII/WaveJSON) |
| `chipchamp/coverage/` | Coverage Service | §8.7 | `model.py`, `ingest.py` (Verilator .dat + functional JSON), `service.py` (holes/attribution/baseline gate) |
| `chipchamp/policy/` | Policy engine | §11 | `diff.py`, `classify.py`, `gates.py` (ladder+gates), `autonomy.py`, `budgets.py`, `acl.py`, `antigaming.py`, `engine.py` |
| `chipchamp/evidence/` | Evidence store | §8.8, §11.3 | `bundle.py` (build+sign+verify+markdown) |
| `chipchamp/tools/` | Agent tool catalog | §9 | `base.py` (Tool+registry+truncate), `context.py`, `design_tools.py`, `verif_tools.py`, `build_tools.py`, `fs_tools.py`, `meta_tools.py`, `catalog.py` |
| `chipchamp/agent/` | Agent core | §8.2, §14 | `providers/` (Anthropic + OpenAI-compatible backends, registry, neutral-transcript translation), `gateway.py` (facade + audit log), `prompts.py`, `loop.py`, `session.py`, `playbooks.py` |
| `chipchamp/` | Wiring + surface | §7.1, §12 | `config.py` (Workspace), `filelist.py`, `cli.py` |

## The trust chain (why a claim is believable)

1. A tool that runs a tool (`sim.run`, `lint.run`, `lec.run`, …) goes through
   `ToolContext.submit` → `JobRunner.submit`, which captures provenance and writes a
   signed-by-construction `JobRecord` under `.chipchamp/runs/<id>/` (`jobs/record.py`,
   `jobs/runner.py`). The model never produces a job record.
2. `report.done` (`tools/meta_tools.py`) calls `PolicyEngine.validate_done`
   (`policy/engine.py`), which re-classifies the change, re-scans for anti-gaming, and
   evaluates every required gate against those job records (`policy/gates.py`). It
   returns `accepted=False` with the blocking gate list if anything is missing/failing.
3. `evidence.bundle` assembles the bundle from the same job records and **signs** the
   machine payload (`evidence/bundle.py`). `verify()` recomputes the hash — any tamper
   (a flipped gate status, an injected result) breaks the signature.

So the path from "agent says done" to "human trusts it" never passes through model
text: it passes through job records the model cannot forge.

## The debug loop (P1/P2), concretely

`sim.run` → `JobRunner` runs Icarus, testbench dumps VCD → `WaveStore.open` indexes it →
`wave.when`/`wave.compare` find the first divergence over a scope → `design.cone`
returns the pruned fan-in slice (crossing instance boundaries via port connections) →
the agent names the mechanism at `file:line`. `demo.py` walks exactly this path.

## Context discipline (§8.9)

- `tools/base.py::truncate` enforces per-result ceilings with explicit markers (FR-CTX-01).
- `DesignDB.module_card` produces the ~200-token L2 summaries (`design/database.py`).
- `agent/prompts.py` builds the cache-friendly stable prefix (rules + DESIGN.md + policy).
- Full logs are never streamed: `JobRunner.grep_log` is a windowed query (FR-JOB-07).

## Extending it

- **A new EDA tool**: subclass `adapters.base.Adapter`, declare a `CapabilityManifest`,
  implement the role method(s) + `parse`, register in `adapters/registry.py`.
- **A new agent tool**: decorate a handler with `@tool(name, …, schema=…)` in a
  `tools/*.py` module and import it from `tools/catalog.py`.
- **A new playbook (skill)**: add a function to `agent/playbooks.py` and a CLI command.
- **A new gate / task class**: extend `policy/gates.py::CLASS_GATES` and `_eval_one`.
- **A new model provider**: subclass `agent/providers/base.Provider` (implement
  `available`/`list_models`/`chat` with neutral-transcript translation) and register
  a `ProviderConfig` preset; the agent loop, `/model` selector and audit log pick it
  up unchanged. OpenAI-compatible servers need no code — add a `[providers.*]` entry.
