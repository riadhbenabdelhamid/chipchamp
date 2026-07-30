---
name: waveform-debug
description: Find why a simulation failed by reading its waveform. Use when a
  sim fails, a signal is X, a handshake stalls, or a value is wrong and you
  need to see what the hardware actually did.
---
# Reading a failing simulation

A waveform is the only place the design's actual behaviour is visible. The
trap is that it is enormous: a smoke test is tens of thousands of edges and a
render squeezed to terminal width turns into unreadable transition marks. So
never open with a picture — **narrow first, then look**.

## 1. Get the time before you get the picture

The testbench already said when it gave up. Read it, don't re-derive it:

    job.log(job=<the failing job>, pattern="CHIPCHAMP_FAIL")

That line usually carries the simulation time (`… @220000`) and the expected
vs actual value. A `sim.run` result also carries `summary` and `errors[]` with
`file:line`. You now know *when* and roughly *what*.

## 2. Narrow to the cycle

Ask for the moment, not the trace:

    wave.when(job=…, expr="full == 0 && level == 8")

Use the failing condition itself as the expression — the first time it holds
is where the design first misbehaved, which is usually earlier than where the
testbench noticed. That gap is the bug.

## 3. Render a window around it

Render tens of cycles, never the whole run, and always include the clock:

    wave.snapshot(job=…, paths=["…clk", "…wr_en", "…full", "…level"],
                  t0=<hit - 100ns>, t1=<hit + 100ns>)

Put the control signals next to the payload they gate (`wr_en` above `full`
above `level`). A bug is usually visible as a *relationship*: a flag flat while
a counter climbs past its limit, a `valid` with no `ready`, a value changing on
the wrong edge.

## 4. Explain it in the design, not the waveform

The waveform says *what*; the design database says *why*:

    design.cone(module=…, signal=<the wrong signal>, depth=1)

Name the driving statement and its `file:line`. A root cause that cannot be
pointed at in the source is a guess.

## 5. Close it properly

Fix the source (`fs.edit`), re-run the same test at the same seed, then
`policy.check` and `report.done`. Gates read job records, so the fix is only
real once a passing job exists. Cite the job ids and the `file:line`.

## If this workspace has the wavelets MCP server

Check your tool list for `mcp.wavelets.*`. If it is there, prefer it for the
rendering steps — it is a full viewer and draws far better chronograms:

- `mcp.wavelets.find_when` — same job as `wave.when`, richer expressions
- `mcp.wavelets.render_waveform` — the chronogram; pass `start`/`end` to
  window it and include the clock in `signals`
- `mcp.wavelets.measure` — for any timing claim (period, delay, setup margin)
  rather than counting columns by eye
- `mcp.wavelets.find_glitches` — X-hazards and runt pulses
- `render_waveform_png` / `_svg` — a raster/vector picture instead of text.
  Only worth it when the shape matters more than the values; the text render
  is portable to logs, transcripts and any terminal.

It needs the VCD **path**, not a job id: take it from the job record's
`artifacts.waves`.

If `mcp.wavelets.*` is NOT in your tool list, this workspace has no wavelets
server configured. Use the built-in `wave.*` tools above and do not mention
wavelets in your answer.
