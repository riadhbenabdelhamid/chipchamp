"""The words the loop says to the model at the moment of drift.

Every guard in the agent loop injects a synthetic user turn when a specific
failure mode shows up: a plan narrated instead of executed, N files written
with no check in between, a lint count that rose, a green tree re-checked
instead of built on. The prose used to live inline in run(), one f-string
per guard, between the bookkeeping that decided when to fire it. Here it is
collected: each function returns the text for one guard, nothing else, so
the wording can be tuned without touching control flow — and the loop
reads as the control flow it is.

Each teaching carries a `guard` tag in the transcript (see loop.run); the
tag names below are the ones tests select on.
"""
from __future__ import annotations


def narration(quoted: str) -> str:
    lead = (f"You wrote: \"{quoted}\" — that is a plan, not an action. Execute "
            f"it NOW as a tool call. " if quoted else
            "You ended with a message but no tool call, and report.done has "
            "not accepted this task. ")
    return (lead
            + "If you are genuinely finished, call report.done now — it is the "
              "ONLY way to complete and it validates your work against the "
              "required gates against real job records. Otherwise take the "
              "NEXT concrete action as a tool call. Do NOT describe what you "
              "will do, and do NOT claim success in prose — act.")


def chunked_write(target: str, length_stops: int) -> str:
    what = target or "the file you were writing"
    return (f"Your turn was cut off at the token limit (finish_reason=length) "
            f"while writing {what}. The content does NOT fit in one turn — "
            f"emitting it again the same way will be cut off again (this has "
            f"now happened {length_stops}x). Write it in PARTS instead: first "
            f"fs.write a compact skeleton (includes, module/class declaration, "
            f"function stubs with TODO bodies), then fill one section per turn "
            f"with fs.edit, verifying as you go. Start with the skeleton NOW.")


def continue_after_cutoff() -> str:
    return ("Your previous message was cut off at the token limit "
            "(finish_reason=length) before you finished. Continue from where "
            "you stopped — if you were about to call a tool, emit that tool "
            "call now. Be concise; do not restart or re-plan from scratch.")


def empty_turn() -> str:
    return ("Your previous reply was empty. Respond now with either a tool "
            "call or a concise final answer as plain message text. No further "
            "internal deliberation. /no_think")


def libdir(path: str) -> str:
    return (f"You wrote {path} into a fetched-library directory. That "
            f"directory holds components copied by lib.fetch — files you "
            f"author there are NOT library components and will confuse you "
            f"later (a previous run wrote its own modules there, judged them "
            f"corrupt, and lost most of its budget). Put your own modules "
            f"under work/rtl/ and re-run the write there.")


def wipe(path: str, green_job: str) -> str:
    return (f"You deleted {path}, which the GREEN checkpoint {green_job} "
            f"contains. If that was not deliberate, "
            f"checkpoint.restore(job='{green_job}') puts the whole green "
            f"state back; otherwise say why the rewrite is necessary and keep "
            f"the change minimal.")


def scope_redo(tool: str, scope: str, prev_job: str, prev_passed: bool) -> str:
    return (f"{tool}({scope}) already has a current result on this unchanged "
            f"tree: {prev_job} {'passed' if prev_passed else 'FAILED'}. "
            f"Re-running it adds nothing — act on the open problem instead.")


def scope_gap(weaker_tool: str, weaker_scope: str, weaker_job: str,
              tool: str, scope: str, job: str, errors) -> str:
    return (f"{weaker_tool}({weaker_scope}) {weaker_job} is green on this "
            f"tree and need not be repeated. {tool}({scope}) {job} FAILED on "
            f"the SAME tree"
            + (f" with {errors} errors" if isinstance(errors, int) else "")
            + " — that is the open problem: fix it, then re-run that scope "
              "only.")


def verify_cadence(writes: int) -> str:
    return (f"You have written or edited {writes} files without running any "
            f"verification. Run lint.run NOW and fix what it reports before "
            f"writing anything new — unverified authoring compounds errors.")


def lint_rose(before: int, after: int) -> str:
    return (f"Lint errors ROSE from {before} to {after} after your last "
            f"change — the previous version was closer. Do NOT rewrite whole "
            f"files. Make one minimal fs.edit that addresses ONLY the FIRST "
            f"reported error, then lint.run again. One error per edit.")


def progress(green_job: str, summary: str, next_step: str) -> str:
    return (f"The workspace is GREEN and UNCHANGED since {green_job} (checks "
            f"on this tree: {summary}) — re-reading or re-checking it adds "
            f"nothing. "
            + (f"The plan's next unfinished step is: {next_step!r}. "
               if next_step else "Your plan has no unfinished step listed. ")
            + "Take it NOW with a constructive tool call (write the next "
              "file, fetch the next component, or report.done if the task is "
              "complete).")


def checkpoint(fails: int, green_job: str) -> str:
    return (f"Your checks have failed {fails} times in a row since "
            f"{green_job} was GREEN. A checkpoint of that green state exists: "
            f"call checkpoint.restore(job='{green_job}') to put the files "
            f"back exactly as they were, then make MINIMAL changes from there "
            f"— one error per edit, re-check after each.")


def oracle(test: str, signature: str, count: int) -> str:
    return (f"The sim '{test}' has now failed {count} consecutive times with "
            f"the IDENTICAL failure signature ({signature!r}). When the "
            "signature never changes no matter what you fix, the TESTBENCH "
            "ORACLE is the prime suspect — re-read the harness before "
            "touching the RTL again, and check it against these laws: (1) a "
            "reference model's valid must NEVER depend on ready — gating "
            "expected valid on m_ready is a handshake modelling error; (2) "
            "elastic / latency-insensitive components (skid buffers, elastic "
            "pipelines, FIFOs) must be checked by STREAM EQUALITY (every beat "
            "accepted at an input handshake appears at the output, in order, "
            "sampled at the DUT's OWN handshakes), never by per-cycle "
            "valid/data comparison against a hand-rolled fixed-latency model. "
            "If the oracle violates either law, fix the HARNESS, not the RTL.")
