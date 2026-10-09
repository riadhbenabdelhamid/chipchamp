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
| `chipchamp/` | Wiring + surface | §7.1, §12 | `config.py` (Workspace), `filelist.py`, `cli/` (package: _core, design, physical, riscv, verify, model, repl, knowledge) |

## The trust chain (why a claim is believable)

1. A tool that runs a tool (`sim.run`, `lint.run`, `lec.run`, …) goes through
   `ToolContext.submit` → `JobRunner.submit`, which captures provenance and writes a
   signed-by-construction `JobRecord` under `.chipchamp/runs/<id>/` (`jobs/record.py`,
   `jobs/runner.py`). The model never produces a job record.
2. `report.done` (`tools/meta_tools.py`) calls `PolicyEngine.validate_done`
   (`policy/engine.py`), which re-classifies the change, re-scans for anti-gaming, and
   evaluates every required gate against those job records (`policy/gates.py`). It
   unions the gates for all applicable task classes. Jobs whose target/configuration
   fingerprint or captured input dependencies have changed are excluded from
   verification and listed as `stale_jobs`; they remain available for diagnosis.
   Regression evidence is tracked by target, test, seed and test configuration,
   and must cover the declared suite plus explicitly requested test/seed pairs.
   It returns `accepted=False` with the blocking gate list if anything is missing/failing.
3. `evidence.bundle` assembles the bundle from the same job records and **signs** the
   machine payload (`evidence/bundle.py`). `verify()` recomputes the hash — any tamper
   (a flipped gate status, an injected result) breaks the signature.

So the path from "agent says done" to "human trusts it" never passes through model
text: it passes through job records the model cannot forge.

Cache keys include step working directories, effective environments, literal
transitive HDL/C includes, memory images, nested native Icarus/Verilator filelists,
declared runner inputs and Verilator's installed support files. Native dependency
outputs supplement provenance; newly discovered inputs never silently promote an
incomplete pre-run manifest to reusable evidence. C++ harness generation and
compilation are separate steps: the generated makefile supplies the flags for a
native preprocessor scan before compilation. System and user headers are hashed;
generated model files are derived outputs. Cocotb inventories the Python environment
before/after execution and uses CPython audit/profiling hooks for runtime dependencies.
Completed traces can enable checkpoint resume; snapshot checks validate observed
reads (including missing files), directory listings and import candidates. Unsupported
runtime activity, custom environment policies/modules, unresolved dependencies and
shell wrappers still disable reuse. These mechanisms assume trusted tool/test code.
Input changes during execution invalidate the result for reuse. External source,
dependency or configuration edits also invalidate the
context's derived coverage, timing and ISA verdicts.

Detached workers inherit farm configuration and share license seats through OS
file locks in the job store. V3 submissions reserve budget before launch, and
completion refreshes the task's records and bills each job once. `mr.prepare`
uses a private Git index to commit only recorded task content to a new branch;
the user's staged files and checked-out branch stay intact.

`verification.py` builds an advisory plan from explicit paths, session edits or
Git changes. It indexes target sources and declared testbench files, walks parent
instances from changed modules, and explains selection or a conservative full-suite
fallback. `verify.plan` and `chipchamp verify-plan` expose the same result without
launching jobs. `regress.run(affected=true)` consumes a freshly computed plan;
completion gates retain the full declared suite. `jobs/reports.py` exports one
JUnit case per requested test/seed plus a JSON report containing the plan and job
records, including submission errors. Reports are written under
`.chipchamp/regressions/`; the `regress` CLI exits nonzero on failures or errors.

Saved plans carry a fingerprint of the selection inputs. `regress --plan` validates
that fingerprint before executing the stored test/seed matrix. `jobs/checkpoint.py`
persists run state atomically as cases complete (completion order, not submission
order), under an exclusive process lock. Resume rebuilds commands from current test
configuration and verifies runner-owned job records, execution keys and evidence
hashes before reusing passed cases. Checkpoint verdicts cannot manufacture evidence.
Runner records identify the regression and restore costs even if its last checkpoint
callback was lost. Missing/failed/stale cases receive fresh attempt directories;
supported adapters retain the workspace as the runtime working directory so relative
memory inputs keep resolving. Other simulator adapters serialize shared outputs.
Full-suite completion policy remains unchanged. SARIF and Markdown exports accompany
JUnit/JSON; the CI command combines lint findings with every regression case, including
infrastructure errors without job records.

`coverage/history.py` stores per-job normalized coverage with a digest pinned in
the runner record. Reads validate the observation, raw coverage artifact and logs.
Comparisons retain bin identities and report denominator changes. Historical hits
in changed files rank selected tests; they never remove tests or seeds. Plans and
checkpoints retain the ordering and its source job IDs across resume.

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
