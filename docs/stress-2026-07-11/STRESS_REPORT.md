# Chipchamp overnight stress test — full report
*Generated 2026-07-11 13:21 CEST · window 02:23→13:21 · LM Studio local models × real EDA flows*

## 1. Local model ranking (best → worst)

Composite = task quality (mean objective score ×100) − tool-error penalty − timeout/model-error penalty − slowness penalty.

| # | model | composite | mean score | scen. done | mean wall s | tool calls | err rate | recovered | empty | timeouts | report.done |
|--:|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | **qwen/qwen3.6-35b-a3b** (Q4_K_M) | 70.0 | 0.74 | 8 | 155.4 | 169 | 9% | 0 | 3 | 0 | 0/0 |
| 2 | **openai/gpt-oss-120b** (MXFP4) | 56.3 | 0.61 | 7 | 78.0 | 91 | 21% | 0 | 1 | 0 | 0/0 |
| 3 | **openai/gpt-oss-20b** (MXFP4) | 53.9 | 0.61 | 8 | 55.3 | 93 | 18% | 0 | 0 | 0 | 1/1 |
| 4 | **qwen/qwen3.6-27b** (Q4_K_M) | 50.6 | 0.71 | 7 | 484.0 | 138 | 12% | 0 | 0 | 1 | 1/1 |
| 5 | **qwen3.5-9b-deepseek-v4-flash** (Q4_K_M) | 46.0 | 0.59 | 7 | 287.5 | 108 | 13% | 0 | 0 | 0 | 0/0 |
| 6 | **nvidia/nemotron-3-nano-omni** (Q4_K_M) | 37.1 | 0.46 | 7 | 287.8 | 111 | 5% | 0 | 2 | 0 | 0/1 |
| 7 | **qwen/qwen3.5-9b** (Q4_K_M) | 36.0 | 0.38 | 7 | 46.7 | 28 | 7% | 3 | 21 | 0 | 0/0 |
| 8 | **liquid/lfm2-24b-a2b** (Q4_K_M) | 22.5 | 0.28 | 4+3dnf | 30.0 | 58 | 33% | 0 | 0 | 0 | 0/0 |
| 9 | **liquid/lfm2.5-1.2b** (Q8_0) | 17.6 | 0.25 | 4+3dnf | 8.4 | 19 | 47% | 0 | 0 | 0 | 1/1 |

## 2. Score matrix (model × scenario)

| model | specQA | triage | libCDC | covClose | rtlEdit | fpga | skillCDC | ppaDW |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| qwen/qwen3.6-35b-a3b | 1.00 | 0.50 | 0.62 | 0.67 | 0.71 | 1.00 | 0.83 | 0.57 |
| openai/gpt-oss-120b | 0.40 | 0.50 | 0.50 | 0.67 | 0.71 | 0.80 | 0.67 | · |
| openai/gpt-oss-20b | 1.00 | 0.67 | 0.50 | 0.67 | 0.71 | 0.80 | 0.50 | 0.00 |
| qwen/qwen3.6-27b | 1.00 | 0.50 | 0.62 | 0.67 | 0.71 | 1.00 | 0.50 | · |
| qwen3.5-9b-deepseek-v4-flash | 0.60 | 0.00 | 0.62 | 0.67 | 0.71 | 1.00 | 0.50 | · |
| nvidia/nemotron-3-nano-omni | 1.00 | 0.00 | 0.50 | 0.50 | 0.43 | 0.60 | 0.17 | · |
| qwen/qwen3.5-9b | 0.20 | 0.00 | 0.50 | 0.50 | 0.14 | 0.80 | 0.50 | · |
| liquid/lfm2-24b-a2b | 0.20 | 0.00 | 0.25 | 0.67 | –(skipped-competence-gate) | –(skipped-competence-gate) | –(skipped-competence-gate) | · |
| liquid/lfm2.5-1.2b | 0.20 | 0.17 | 0.12 | 0.50 | –(skipped-competence-gate) | –(skipped-competence-gate) | –(skipped-competence-gate) | · |

## 3. Platform feature sweep (no model): 27/30 pass

| feature | result | wall s |
|---|---|--:|
| adapters | ✅ | 3.5 |
| index | ✅ | 0.2 |
| hier | ✅ | 0.1 |
| module_card | ✅ | 0.2 |
| cdc | ✅ | 0.2 |
| fsm | ✅ | 0.2 |
| diagram | ✅ | 0.2 |
| cone | ✅ | 0.3 |
| lint | ✅ | 0.3 |
| sim_sv | ✅ | 0.2 |
| sim_cocotb | ✅ | 0.8 |
| sim_uvm | ✅ | 122.2 |
| triage | ✅ | 0.4 |
| coverage | ✅ | 0.3 |
| synth | ✅ | 0.2 |
| fpga_ice40 | ❌ | 0.1 |
| fpga_vivado | ❌ | 0.1 |
| pnr_librelane | ✅ | 63.5 |
| optimize_brief | ❌ | 0.1 |
| uvm_scaffold | ✅ | 0.2 |
| regmap | ✅ | 0.2 |
| packs | ✅ | 0.1 |
| skills_list | ✅ | 0.2 |
| library_list | ✅ | 0.2 |
| library_show | ✅ | 0.2 |
| library_search | ✅ | 0.1 |
| library_fetch | ✅ | 0.2 |
| jobs | ✅ | 0.1 |
| dashboard | ✅ | 0.1 |
| ci_gate | ✅ | 107.8 |

## 4. Issues found & fixed tonight

# Findings log — chipchamp overnight stress test (2026-07-11)

Status: FIXED = committed to master · OPEN = reported only · NOTE = observation

## F1 — /library feature absent (requested by user) — FIXED (214d837)
- **Area**: platform / reuse
- **Symptom**: no way to point chipchamp at an IP component library; riscvier's manifest built "for agentic tooling" had no consumer.
- **Fix**: full feature — `chipchamp/library.py` (layered discovery: config `[library].dirs` + `.chipchamp/library.json` registry), 4 tools (`lib.list/search/info/fetch`), CLI group `chipchamp library …` (+ `/library` in-session), system-prompt index section, policy-gated fetch with recorded edits, 10 tests incl. live-corpus test.

## F2 — riscvier manifest hygiene — FIXED (data)
- **Area**: user library data
- **Symptom**: 4 components with RTL missing from manifest.json (`fifo_async`, `apb_cdc`, `ahb_pkg` + stale count); `axil_cdc` has no metadata at all.
- **Root cause**: YAML flow-mapping breakage in module .md frontmatter — unquoted `desc:` values containing `,`/`>=` (fifo_async) and `[`/`]` (ahb_pkg); generator skips bad files with a warning.
- **Fix**: quoted the desc strings; `make manifest` → 120 modules (was 117). Backup of old manifest in scratchpad. Then authored full `axil_cdc.md` metadata (params/ports/clocks/resets/presets) → **121 components**. Follow-up: the file's `status`/`verification` fields currently advertise TB/formal/packaging collateral that isn't on disk yet (module ships `rtl/` only) — align status with shipped collateral (or add the TBs) before relying on it, then re-run `make manifest` so the manifest matches.

## F3 — reasoning models end tasks silently (empty turns) — FIXED (e2b2ea6)
- **Area**: agent loop / local models
- **Symptom**: qwen3.5-9b burned max_tokens=4096 in `reasoning_content` (finish_reason=length) or emitted reasoning-only turns at finish=stop → loop treated "no text, no tool calls" as normal end → task ends with NO output (score 0).
- **Fix**: parse `reasoning_content`/`reasoning` + inline `<think>` into `ModelResponse.reasoning`; bounded nudges ("respond with a tool call or final answer… /no_think"); after 3 empties, salvage the answer from the reasoning channel (labeled) instead of silence; `[model] max_tokens` config + `CHIPCHAMP_MODEL_MAX_TOKENS` env; per-run `empty_turns`/`length_stops` counters surfaced by CLI.
- **Impact measured**: spec_qa score 0.0 → 1.0 on qwen3.5-9b.

## F4 — server-parsed tool calls missed → "model can't call tools" — FIXED (e2b2ea6)
- **Area**: agent loop / local models (the big one)
- **Symptom**: qwen3.5-9b emitted `<tool_call><function=fs_list><parameter=dir>…` as TEXT; LM Studio's template parser produced no structured tool_calls; chipchamp concluded the model made no call.
- **Fix**: `agent/toolcalls.py` — conservative text-recovery of embedded calls (JSON style, function-tag style incl. truncated tails, fenced JSON; known-tool-name gated so prose mentions never execute), wired into the loop before the empty-turn path; `recovered_calls` metric.

## F5 — regression/triage mis-dispatch UVM + cocotb tests — FIXED (693c291)
- **Area**: platform / verification core
- **Symptom**: `triage` on the example SoC: 9/15 passed, 6 errored ("compile failed": `uvm_macros.svh not found`) — the same tests pass standalone via `sim`.
- **Root cause**: `regress.run` and the triage playbook re-implemented dispatch with the default sim adapter (icarus) — UVM tests never got the verilator+uvm path, cocotb tests never got their Python runner.
- **Fix**: extracted `_plan_for_test()` as the single source of truth (sv/uvm/cocotb), used by sim.run, regress.run, cov.run, triage. Triage now 15/15.

## F6 — cocotb functional coverage silently wrong (100% on empty) — FIXED (693c291)
- **Area**: coverage service
- **Symptom**: `cov counter_cov_cocotb` ran verilator line-coverage on a nonexistent SV top → empty model → **summary total=100%** (silent green).
- **Fix**: cov.run routes cocotb tests through the cocotb runner and ingests the exported cocotb-coverage XML (26 real bins, 50% with genuine holes); `CoverageModel.total()` returns 0.0 for an empty model ("no data ≠ fully covered").

## F7 — errored tests invisible in regression reports — FIXED (693c291)
- **Area**: UX / reporting
- **Symptom**: triage JSON said "6 errored" but nowhere WHICH tests or why (clusters only cover failures).
- **Fix**: `RegressionResult.errored_tests` ({test, seed, job, reason}) surfaced in regress.run + triage outputs.

## F8 — fs.read unhelpful on wrong path — FIXED (e2b2ea6)
- **Symptom**: models guess repo-relative paths (`examples/soc/rtl/…`) and get bare "no such file".
- **Fix**: error now hints "paths are relative to the workspace root; fs.list shows the tree".

## F9 — `test_orchestrator.py::test_fanout_merges_results_and_runs_parallel` flaky under load — OPEN
- Fails in a full parallel suite run, passes alone (2×). Pre-existing timing sensitivity; ironic given the platform's own flake detection. Needs a deterministic wait in the test.

## F10 — pytest suite lacks timeout plugin — NOTE
- `--timeout` not available (pytest-timeout not a dep); a hung EDA call can wedge CI.

## N1 — triage summary counts seeds not tests — NOTE
- "9/12 passed" with 4 tests × 3 seeds reads as 12 "tests"; fine once known, but the string could say "runs".

## N2 — model.json vs env precedence — NOTE (docs match behavior; fine)

## F12 — one transient provider glitch kills a whole task — FIXED (d46d7ff)
- **Area**: agent loop / providers
- **Symptom**: gpt-oss-20b mid-triage (3 good tool calls in) → LM Studio engine 500 "model produced output that does not match the expected peg-native format" (grammar-constrained decode rejecting one bad sample), surfaced as HTTP 400 → loop aborted the task instantly.
- **Fix**: `_call_with_retry` — bounded retries (3 attempts, backoff) for retryable classes (5xx, 400-format/peg-native, 429, connection drops); auth/404 never retried; timeouts never retried (slow is a fact, not a glitch). Per-run `call_retries` counter.

## F13 — fs.read's numbered render breaks fs.edit for every model — FIXED (d46d7ff)
- **Area**: workspace tools (the second big one)
- **Symptom**: models copy edit anchors from fs.read output (`"   64      # one early clr pulse…"`) — the 5-digit+2-space prefix and re-guessed indentation are not the file's bytes, so fs.edit "old string not found" **death loops** (gpt-oss-120b: 15 consecutive steps on one edit; every model failed coverage_close on it).
- **Fix**: fs.edit now (1) whitespace-lenient line matching when unique, with indentation reconstruction; (2) strips pasted line-number prefixes when unambiguous; (3) on miss, returns `nearest_match.raw` — the actual file bytes near the closest line — so the retry can be exact; ambiguity still refuses. 5 new tests.

## F11 — harness calibration (meta): step budgets + validator credit — FIXED (harness)
- spec_qa max_steps 10 was too tight for verbose planners (27b hit the cap mid-work, no final answer); prose validators credited plan echoes. Fixed: final-answer extraction (`final_answer()`), steps 10→14; spec_qa re-run for all models at end of pass for consistency.

## N3 — system prompt size with big skill corpora — NOTE
- 97 skills → ~10 KB skills index in EVERY call (~2.5k tokens). `_INDEX_CAP` (16k chars) helps, but for 200+ skill corpora consider `index="off"` + skill.list on demand, or embedding-based trigger match (embedding model available in LM Studio).


### Commits
```
d46d7ff fs.edit resilience + model-call retry: whitespace-lenient unique matching, pasted-line-number recovery, teaching errors with raw bytes; retry transient provider glitches (LM Studio peg-native 400/500)
693c291 One dispatcher for every test runner: regression/triage now route UVM+cocotb correctly; cocotb functional coverage in cov.run; empty coverage is 0%, not green; errored tests named in reports
e2b2ea6 Local-model robustness: recover text-embedded tool calls, nudge empty reasoning turns, surface trapped answers, [model] max_tokens knob
214d837 IP library (/library): manifest-driven component reuse — registry, lib.* tools, CLI group, prompt index
```