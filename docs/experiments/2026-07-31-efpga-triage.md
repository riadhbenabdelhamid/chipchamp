# 2026-07-31 — eFPGA triage: is my program broken, or my chip?

`demo_efpga_triage.py`. A user design that built yesterday fails to route
today, and the fault is in the *fabric's source*: a "switch-matrix mux
pruning" commit collapsed 134 LUT4AB muxes to single inputs. Generation
still validates — every destination keeps a driver — but the routing graph
quietly drops from 206,424 to 145,104 edges and nextpnr blames the innocent
design. The deliverable is the DIAGNOSIS, filed as a `note.add(root_cause)`
and graded against ground truth.

Ten agentic runs, one model (qwen3.6:35b), one injected fault. What makes
this campaign worth keeping is that **every run's failure was information**:
seven of the ten failed *because of something the platform, the harness, or
the grader was doing wrong*, and each of those walls became a committed fix.
Raw records: `2026-07-31-efpga-triage.json`.

## The campaign, run by run

| # | experiment | setup delta | result | what it actually proved |
|---|---|---|---|---|
| v1 | E-8f02a000a2 | generated-artifact fault | "closed" | the model healed the chip by accident (`with_fabric=true`) before observing the failure — clean-rebuild is an escape hatch, so v2 moved the fault into source |
| 1 | E-b21a35bd20 | v2, unblinded | note filed, **zero jobs** | the notebook digest carried the scripted run's root-cause note into the system prompt; the model paraphrased its own briefing. Blinding was born here |
| 2 | E-f8adf7ecbc | + notebook blinded | 3/6 capped | honest but stuck: ~10 calls guessing job ids against mute errors, an "unknown tool" storm that was really unlabeled subagent role-denials. The five-fix list came from this run |
| 3 | E-1ad3ed6239 | + fix list (job.list…) | "closed", wrong | closed on **archaeology**: the cross-campaign job archive held a healthy-era pass and a broken-era fail sharing one (empty-manifest) input hash → "nondeterministic nextpnr". Two real bugs exposed: constant eFPGA input hashes, un-blinded job store |
| 4 | E-0f7398f32b | + archive blinded | 2/6 | the model *correctly proved* its workspace contained no eFPGA project: `fs.list`/`fs.grep` defaulted to `*.sv` and the whole vertical (.v/.csv/.list) was invisible. Every earlier run had been routed around this hole by the archive |
| 5 | E-2b1578f68a | + fs defaults fixed | 2/6 capped | my harness bug: blinding renamed `runs/` under a live runner; id allocation crashed every submit. Runner now mkdirs; the demo leaves an empty store |
| 6 | E-ee5540a55e | + store functional | **4/6 capped** | the honest datapoint: reproduced twice, ruled out stale state, design untouched — ran out of 40 steps mid-localization of the unroutable net |
| 7 | E-04ec81cd76 | grader v1 | "6/6", wrong | closed on a **wrong mechanism**: probed the (independently broken) harden flow, and its yosys crash satisfied every shape-check — "reproduced" by the wrong failure, "ruled out" by a different flow, note matched on the word "tile". The grader got teeth here: same-flow evidence only, mechanism words only |
| 8 | E-4257a03757 | sharp grader, 100 steps | 3/6 | stopped by the token guard at 4096 max_tokens, mid-honest-trace; 8192 became the calibration |
| 9 | E-533b707505 | + 8192 tokens | 3/6, closed wrong | fast static archaeology: the `Global_Clock` red herring (a synth_fabulous-provided primitive with no in-tree definition) became "the fabric lost its clock tile". The sharp grader correctly rejected what grader v1 would have accepted |

## What the campaign banked

Committed fixes (all live before the grow campaign started): lenient
tool-name resolution, role-aware denials, labeled subagent events, teaching
errors on every id-taking tool, `job.list`, fabric-state-aware input hashes,
fs defaults that see the whole vertical, store-dir-proof id allocation, and
a grader that demands same-flow evidence and mechanism words.

Method lessons now encoded in the demo itself: **blind the notebook** (the
answer key rides the system prompt), **blind the job archive** (models
prefer archaeology to science when both are offered), and **grade
mechanisms, not shapes** (two runs closed by satisfying the letter of the
axes with the wrong causal story).

Environment discovery: `run_FABulous_eFPGA_macro` (harden) is broken here —
oss-cad-suite's yosys removed the `-y` flag FABulous passes; all 17 tiles
die identically. Demo B is blocked on toolchain alignment, and until the
adapter learned to say so, that loud irrelevant failure captured two runs'
narratives.

## Where it ends

The walls are gone; runs 8–9 hit nothing but the model. qwen3.6:35b's triage
judgment is genuinely high-variance: run 6 did flawless science and ran out
of clock; run 9 had every affordance and skipped the science. A close-rate
estimate on these now-honest terms needs a small-N campaign — each run is
~10–35 min.


## 2026-08-04/05 — the close-rate campaign (5 runs, 100-step guard)

Five blind runs against the unchanged fault, sharp grader, 8192-token
turns. The step guard at 100 — far above the old 40 — is the variable that
mattered; nothing else changed.

| run | experiment | verdict |
|---|---|---|
| 1 | E-570664ed24 | closed, 51 steps (axis panel lost to a machine reboot) |
| 2 | E-0f90eaa6a1 | **6/6** — full ladder, mechanism-naming note |
| 3 | E-3e966c5272 | **6/6** in 29 min — staged elimination table (synth ✓ place ✓ route ✗, "no PIP paths connect any placed LC tile") |
| 4 | (rc=0) | **6/6** |
| 5 | E-e7402bca5a | 5/6 — right note, right attribution, skipped the rebuild-elimination beat |

**Close rate 5/5; full-ladder 3/4 of verdict-known runs.** Zero
wrong-mechanism closes, zero archaeology, zero design edits. The variance
the 40-step campaigns attributed to the model was substantially the budget:
given room, the same model runs the same honest arc almost every time.


## 2026-08-08 — multi-model close rates (same instrument, same faults)

| model | triage | grow | dominant failure mode |
|---|---|---|---|
| qwen3.6:35b (MoE, ~3B active) | **5/5 closed** (3× full 6/6) | **3/5 closed** (judgment 5/5) | CSV execution in the two caps |
| nemotron-3-nano:4b | 0/5 | 0/5 | turns never complete: truncation-guard stop at ~10 steps, 0 jobs — at BOTH 8192 and 16384 output tokens |
| lfm2.5:8b | 0/5 | 0/5 | protocol failure: emits tool-call-shaped JSON as prose, never drives the tool API; 0 jobs, seconds per run |

Twenty comparison runs, zero gaming, zero design violations, and three
cleanly distinguishable failure classes: the 35B fails on *execution
mechanics*, the 4B on *turn economics*, the 8B on *tool protocol*. These are
router-profile facts now, not vibes. One boundary showed up statistically:
models that do nothing can still close investigation-class tasks (no edits →
no gates owed) — the case for requiring a cited failing job on any
root_cause close. gpt-oss-120b's leg awaits an LM Studio session.
