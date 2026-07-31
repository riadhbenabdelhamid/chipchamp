# 2026-07-31 — the waveform demo, scripted and agentic

`demo_waveform.py` runs one bug two ways: a hardcoded walkthrough (mode 1) and
a model-driven run (mode 2), each labelled on screen as what it is. The bug is
a one-character off-by-one in `sync_fifo`'s `full` flag — `count > DEPTH`
instead of `count == DEPTH` — chosen because it is legible in a waveform as a
relationship: the flag flat at zero while `level` climbs to the configured
depth.

Raw records: `2026-07-31-waveform-demo.json` (the `demo-agentic` experiments).

## Mode 1 — scripted: green

Six scenes, ending `report.done: ACCEPTED`, ladder 5/5, exit 0. The
centrepiece is the windowed analyzer view:

    wave.when(level == 8 && full == 0) → first true at t=185000

    ⌁ waves · around t=185000 · 24 cycles of clk
    wr_en ▔▔▁▁▔▔▁▁▔▔▁▁▔▔▁▁▔▔▁▁▔▔▁▁▔▔▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁
     full ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁
    level 2═══╳3══╳4══╳5══╳6══╳7══╳8══╳7══╳6══╳5══╳4══╳3══

The time comes from `wave.when`, not from an assumption — the first run of the
demo rendered the tail of the trace and showed the *aftermath* (level at 9,
full asserting) under narration describing the onset. The picture contradicted
the words, which is the one thing a demo must never do; `analyzer_view` gained
`around=` because of it.

## Mode 2 — agentic: seven runs, one honest table

| run | model | outcome | steps | what actually happened |
|---|---|---|---|---|
| 1 | devstral:24b | timeout · 0 steps · 649s | 0 | wrong gateway ref — `provider:model` passed where a bare model name was wanted; "no model configured", **demo still printed "it closed the task"** (see below) |
| 2 | devstral:24b | timeout · 0 steps · 589s | 0 | prompt ~11.5k tokens vs ollama's default `num_ctx` 4096 — server accepts and grinds, no error anywhere |
| 3 | devstral:24b | timeout · 0 steps · 590s | 0 | model cold; a 75 GB neighbour was evicted + 14 GB loaded *inside* the timed request |
| 4 | laguna-s-2.1 (75 GB) | timeout · 0 steps · 1830s | 0 | warm alone took 640s; then nothing within the budget. Not viable interactively on this machine |
| 5 | nemotron-3-nano:4b | **answered** · 51s | 6 | **never actually simulated**: passed the test name as a job id to `job.log`/`job.status`, then hallucinated `sim.run(wave="True")` (singular, string) and did not retry; answered anyway |
| 6 | nemotron-3-nano:4b | *(no record — killed by operator)* | ≥6 | partial transcript below: the most competent run, contaminated at the end by a second demo process racing the same workspace |

Run 6's partial transcript, verbatim — a real investigation:

    → sim.list_tests({"tag": ""})
    → sim.run({"test": "fifo_smoke", "waves": true, "plusargs": []})
    ▪ J-0249 sim.run failed · expected full after 8 pushes
    (the REPL auto-rendered the failing waveform here: full toggling,
     level reaching 9, the testbench's errors counter stepping 1→2→3→4)
    → wave.value(job=…)          ✗ bad arguments (parameter is `run`)
    → job.log({"job": "J-0249", "pattern": "fifo data mismatch"})
    → design.module({"module": "soc_rtl"})   ✗ not in index
    → fs.list({"dir": "/", "pattern": "*.v"})

No model closed the task (`report.done` accepted) on this machine. That is
the honest current answer to "can a local model do this end to end here", and
the demo's verdict panel reports it as five independent axes rather than one
flattering bit.

### Run 5, corrected on closer reading

The first version of this table said run 5 "ran the sim". It did not — the
record's `job_detail` shows `{'job': '', 'status': '?'}`; its one `sim.run`
was rejected on the hallucinated argument. Two of its three calls also passed
the TEST NAME where a JOB ID (`J-0249`-style) belongs. `job.status` errored
honestly; **`job.log` returned `{"hits": []}`** — indistinguishable from "the
job exists and nothing matched" — so the model believed it had consulted a log
that was never read, and the demo's "consulted the waveform / job log" axis
gave it a ✓ for that empty grep. The same vacuous-success trap as the gates,
one layer down: both the tool and the verdict axis accepted an action that
touched nothing.

The stumbles across both nemotron runs share one shape: the *strategy* is
sound (list → run → inspect → localize) and every failure is API surface —
parameter names (`wave`/`job` for `waves`/`run`), id-vs-name confusion,
guessed identifiers (`soc_rtl`, an invented `filter` argument, `fs.list` on
`/`). That is why the durable fixes are schema-side: enums, `accepts` lists on
rejection, and errors on misses instead of empty successes.

## Runs 7–8 — rerun with the teaching errors in place

| run | outcome | steps | calls | what changed |
|---|---|---|---|---|
| 7 | answered · 41s | 3 | 1 | hit the new job.log error; **chose the right next tool** (`sim.list_tests`) but emitted the call as a bare JSON message the loop read as prose — recovery format E added because of it |
| 8 | answered · 52s | 10 | 6 (2 failed) | the compounding run: after the job.log teaching error listed real ids, **its next call used one (`J-0311`)** — the hint demonstrably drove recovery. Also ran a real (failing-to-compile) sim and a design.hierarchy |

Run 8 settles the question commits `0176a2e`/`cf946db` left open: a weak
model DOES use a corrective error, when the correction carries the answer.
The trajectory across runs 5→7→8 — one blind call, then a right-idea-lost,
then six calls with mid-run recovery — is the error-side fixes compounding,
same model, same prompt.

What still fails is now genuinely model-side: run 8 hallucinated test names
(`fifo_kmwf_smoke`, `fifo_kmwf_original_smoke`) *after* calling
`sim.list_tests`, which had just returned the real ones — identifier
corruption, not missing information; asked for permission it already has
("I need your explicit permission to start probing"); and ended on three
empty turns of planning prose. 91,912 tokens for 52s of work. No RTL edit,
no close. That is the honest ceiling of a 3-nano-4B here, and the demo's
verdict panel states it as such.

## Runs 9–12 — laguna-xs, then the close

**Run 9 (laguna-xs.2, 23 GB): three warm-up attempts, no agent step.** The
load alone exceeded the 900s warm budget; a second attempt queued behind the
first's still-running generation; a third was killed by the operator. Two
demo defects fixed as a result: `warm_up` now skips when the model is already
resident (`/api/ps`) instead of queueing, and caps its reply with
`num_predict` — an uncapped "ok" invites a reasoning model to write an essay
at local-inference speeds, which is what both "warm timeouts" actually were.
No capability datapoint for this model exists yet; what is established is
that its *load* is ~20× slower than qwen3.6:35b's at the same file size.

**Runs 10–12 (qwen3.6:35b-a3b): the walls come down one per run, then it
closes.**

| run | outcome | steps | calls | what stopped it |
|---|---|---|---|---|
| 10 | capped · 607s | 16 | 22 | our bug: `wave.value` with `time` as a digit-STRING hit a raw `'<' not supported` TypeError; the accepts hint could not help (names were right) and six identical retries ate the budget → schema-driven scalar coercion added at the exec boundary |
| 11 | capped · 266s | 16 | 15 (3 failed, all recovered) | nothing but the step budget — a thorough investigator at ~1 productive call per step, capped mid-signal-hunt |
| 12 | **completed** · 334s | 30 | 34 | **nothing. First autonomous close of the campaign.** |

Run 12, end to end: reproduced the failure (served from the job cache — the
same record as run 11's sim, so the baseline cost nothing), read the log by
real job id, read testbench then RTL, recovered a doubled path via `fs.grep`,
**loaded the wave group via `tools.load` when it noticed it lacked schemas**,
diagnosed the flag, edited `sync_fifo.sv`, watched the gate ladder fire at
1/5, re-simmed to green, ran lint, **loaded the cov group after reading
`coverage_baseline` off its own ladder**, ran coverage, and had `report.done`
ACCEPTED at 5/5. Its fix was `count >= DEPTH` — a different correct repair
than the original `count == DEPTH` (equivalent in reachable state, arguably
more defensive), which the gates accepted on evidence rather than spelling.

Two design claims this run substantiates beyond the fixes themselves: the
disclosure menu is actionable mid-task (the model pulled in `wave` and `cov`
groups exactly when it discovered the need), and the live gate ladder is not
decoration — the model read its remaining obligations off it and paid them in
order.

## The finding that matters most

Run 1's demo printed **"it closed the task" for a model that executed zero
steps**. The success check was `gate_status().all_passed` — and an *empty
change set passes the gates vacuously*, because no edits means no
obligations. A success criterion satisfiable by inaction is worse than none,
and it is exactly the failure mode `report.done` exists to prevent; the demo
now judges on evidence (any action / consulted waves / edited RTL / test
passing / accepted report.done) and reserves "closed" for the last.

## Eliminated causes, in order — each one a fix

1. **Gateway ref parsing** — `--model ollama:devstral:24b` must split into
   (provider, model) before `_build_gateway`; passed whole, it silently
   yields an unavailable gateway on the default provider.
2. **Context window** — chipchamp's system prompt + tool schemas is ~11.5k
   tokens full / ~5.4k with disclosure; ollama defaults `num_ctx=4096` and
   *accepts* the oversized request, grinding instead of refusing. The loop now
   measures its prompt against the declared window and warns before sending,
   naming the cheaper fix (`tool_disclosure`) first.
3. **Cold load inside the timed request** — the demo now pre-warms via
   `/api/generate` with `keep_alive`, stating on screen why.
4. **Workspace contention** — running the test suite while the demo held the
   injected bug produced a spurious suite failure; one workspace, one writer.
5. **Self-matching process waits** — `until ! pgrep -f "demo_waveform…"`
   loops matched *each other's* command lines and never terminated, which is
   how a second demo process ended up racing the first. (Operator error,
   recorded so it is not repeated.)

What remains after all five: devstral and laguna genuinely do not produce a
first step within 600–1800s on this machine, while the same demo path carries
a 4B through a real investigation in 51s. The bottleneck is the machine and
the model, not the loop — proven by running the identical code with only the
model changed.

## Smaller fixes the campaign forced

- `pick_signals` drops never-toggling signals (by value history, not
  `var_type` — parameters *and* frozen integers), recovering 3 of 8 rows on
  the example SoC; a signal stuck at x/z is kept, since hiding a defect is
  the one thing a waveform picker must not do.
- `analyzer_view(around=…)` + centre-preserving trim on narrow terminals.
- A `TypeError` from a tool call now returns `accepts` + `required` from the
  schema — run 5's model got only *"unexpected keyword argument"*, which is
  true and unactionable. Observed so far: the next run's model chose a
  different tool rather than retrying; whether the hint drives recovery is
  still open.

## For a live audience

Use `nemotron-3-nano:4b` for mode 2: it is fast enough to watch, it reaches
the tools, and its stumbles are legible — which is the demo's stated point.
The large local models produce nothing to watch. A frontier API model is the
other honest option; nothing in the demo path is local-specific.
