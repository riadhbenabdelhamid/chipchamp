"""The agent loop (SPEC §8.2): plan → act via tools → observe → iterate.

Model-driven control flow over the tool catalog, provider-neutral. The loop keeps
a JSON-safe *neutral transcript* (see :mod:`chipchamp.agent.providers.base`); each
provider translates it to its own wire format, so the same loop drives Anthropic,
OpenAI, or a local server. Tool-result provenance is fed to the context audit log
each turn. With :class:`NullGateway` the loop reports that a scripted playbook
should be used instead.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..policy.budgets import BudgetExceeded
from ..tools import all_tools
from ..tools.context import ToolContext
from . import disclosure, working_set
from .gateway import ModelGateway
from .prompts import build_system_prompt, policy_summary
from .providers.base import ModelResponse
from .session import Session


class AgentLoop:
    # meta tools that finish/report a task — never gated by plan mode
    _GATE_EXCLUDE = {"report.done", "evidence.bundle", "policy.check"}

    def __init__(self, ctx: ToolContext, gateway: ModelGateway,
                 session: Optional[Session] = None, max_steps: int = 40,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 tools: Optional[dict] = None,
                 system_suffix: str = "",
                 approver: Optional[Callable[[str, dict, str], bool]] = None,
                 gate_permissions: Optional[set] = None,
                 router=None, need: str = "rtl_author",
                 disclose: bool = False, context_budget: int = 0):
        """`tools` restricts the catalog (least-privilege subagents, FR-MA-01);
        `system_suffix` appends role instructions to the system prompt. In plan
        mode `gate_permissions` (e.g. {"write","submit"}) names the permission
        tiers that must be `approver`-approved before a tool runs.

        `router` (a ModelRouter, optional) enables model auto-switching: when the
        current model hits a failure signature (refusal / timeout / stall on
        failing checks / give-up), the loop hot-swaps to the next fallback in the
        pool and continues on the same transcript. `need` is the task's default
        capability for that ranking. A None/disabled router is a no-op."""
        self.ctx = ctx
        self.gateway = gateway
        # Expose the gateway on the context so a tool can delegate to a
        # sub-agent (the `research` web tool spins up a bounded sub-loop), and
        # the loop itself so `tools.load` can widen the live catalog.
        ctx.gateway = gateway
        ctx.loop = self
        self.router = router
        self.need = need
        self.session = session
        self.max_steps = max_steps
        # Shield the loop from its observer: a crash in a UI/event callback
        # (e.g. rendering an unexpected result shape) must never kill an
        # autonomous run — one did, via a dict-valued summary in job_done.
        raw_cb = on_event or (lambda kind, data: None)

        def _safe_event(kind: str, data: dict) -> None:
            try:
                raw_cb(kind, data)
            except Exception:
                pass

        self.on_event = _safe_event
        self.tools = tools if tools is not None else all_tools()
        self.system_suffix = system_suffix
        self.approver = approver or (lambda name, args, perm: True)
        self.gate_permissions = gate_permissions or set()
        # `_name_map` covers the WHOLE catalog even under disclosure: a model
        # that names an unloaded tool correctly should have it work, not be
        # told it does not exist. Only the schemas sent are narrowed.
        self._name_map = {n.replace(".", "__"): n for n in self.tools}
        self.disclose = bool(disclose)
        self._loaded = (disclosure.core_names(self.tools) if self.disclose
                        else set(self.tools))
        self._prompt_dirty = False
        # 0 = never compact (the old unbounded behaviour, kept for tests and
        # for anyone who would rather hit a hard wall than lose a body)
        self.context_budget = max(0, int(context_budget or 0))
        self._refresh_api_tools()

    def _refresh_api_tools(self) -> None:
        """Neutral tool schema for the provider: {name (api-safe), description,
        schema}. Rebuilt whenever the loaded set widens."""
        self._api_tools = [
            {"name": n.replace(".", "__"), "description": t.description,
             "schema": t.schema}
            for n, t in self.tools.items() if n in self._loaded]

    def load_groups(self, groups: list[str]) -> dict:
        """Widen the live catalog by group (the `tools.load` tool's body)."""
        known = disclosure.groups_of(self.tools)
        added, unknown = [], []
        for g in groups:
            if g not in known:
                unknown.append(g)
                continue
            new = [n for n in known[g] if n not in self._loaded]
            self._loaded.update(new)
            added.extend(new)
        if added:
            self._refresh_api_tools()
            # the prompt's "not yet loaded" menu is now stale — a menu that
            # still lists a tool the model is holding reads as a contradiction
            self._prompt_dirty = True
            self.on_event("tools_loaded", {"groups": groups, "added": added})
        out = {"loaded": sorted(added),
               "available_now": sorted(self._loaded)}
        if unknown:
            out["unknown_groups"] = unknown
            out["known_groups"] = sorted(known)
        return out

    def system_prompt(self, task: str = "") -> str:
        digest = self.ctx.ws.design_md[:4000]
        base = build_system_prompt(self.ctx.ws, digest,
                                   policy_summary(self.ctx.policy), task=task)
        if self.disclose:
            menu = disclosure.deferred_summary(self.tools, self._loaded)
            if menu:
                base += "\n\n" + disclosure.DISCLOSURE_RULE.format(menu=menu)
        if self.system_suffix:
            base += "\n\n## Role\n" + self.system_suffix
        return base

    def run(self, user_prompt: str) -> dict:
        if not self.gateway.available:
            return {"text": "no model configured; run `chipchamp model` to pick one, "
                    "or use a scripted playbook (chipchamp triage / lint)", "steps": 0}
        transcript: list[dict] = list(self.session.messages) if self.session else []
        # `input: True` marks a GENUINE user turn — the loop also injects
        # synthetic "user" nudges/observations, so this flag is how a resumed
        # session tells real requests apart (for replay + up/down history).
        # Providers read only role/content/tool_calls, so the extra key is inert.
        transcript.append({"role": "user", "content": user_prompt, "input": True})
        system = self.system_prompt(task=user_prompt)
        final_text = ""
        step = 0
        empty_turns = 0
        length_stops = 0
        recovered_calls = 0
        narration_nudges = 0   # consecutive text-only turns nudged to act
        reported_done = False  # report.done accepted → text is a real answer
        made_mutating_call = False  # wrote a file / ran a job → needs report.done
        last_reasoning = ""
        known_names = set(self._name_map) | set(self._name_map.values())
        call_retries = 0
        self._tried = set()          # model refs already used/skipped this run
        self._switches = []          # [{from, to, reason}] for the return dict
        self._models_used = [self.gateway.ref]
        stall = 0                    # consecutive failing checks with no progress
        last_lint_err = None         # for detecting productive vs stuck iteration
        sim_sig = None               # (test, normalized summary) of last sim fail
        sim_sig_count = 0            # consecutive sim fails with IDENTICAL sig
        sim_sigs_nudged = set()      # signatures already flagged as oracle-suspect
        # proactive layer: start on the best model for this task's need (not just
        # switch on failure). Reactive stall-switching still hands debug work to
        # the best debugger from here.
        if (self.router and self.router.cfg.enabled
                and self.router.cfg.layers.get("proactive")):
            best = self.router.pick(self.need)
            if best and best != self.gateway.ref:
                gw = self.router.build_gateway(best, like=self.gateway)
                if gw is not None and gw.available:
                    self.on_event("model_switch", {"from": self.gateway.ref,
                                                    "to": best, "reason": "proactive"})
                    self.gateway = gw
                    self._models_used.append(best)
        for step in range(self.max_steps):
            # the token ceiling is checked HERE, not advertised in the prompt:
            # telling a model its budget is not a budget (§12.4 FR-BUDG-01)
            ok, why = self.ctx.ledger.can_spend_tokens()
            if not ok:
                self.on_event("budget_block", {"name": "model", "reason": why})
                final_text = final_text or why
                break
            if self._prompt_dirty:
                system = self.system_prompt(task=user_prompt)
                self._prompt_dirty = False
            # Working set (§8.9): evict old tool-result bodies once the
            # transcript outgrows its budget. NOT summarization — every evicted
            # body is re-fetchable through the job id left in its place, which
            # is the one structural advantage a hardware agent has here.
            if self.context_budget:
                transcript, moved = working_set.compact(
                    transcript, budget_chars=self.context_budget)
                if moved["compacted"]:
                    self.on_event("compacted", moved)
                    if self.session:
                        self.session.messages = transcript
            # the size of what is about to be sent rides on the event: it is the
            # number that says whether a long run can survive its own context,
            # and nothing else in the loop was measuring it.
            self.on_event("thinking_start", {"model": self.gateway.ref,
                                             **transcript_size(transcript)})
            try:
                resp = self._call_with_retry(system, transcript)
            except Exception as e:  # API/network/auth/timeout — fail cleanly
                self.on_event("thinking_done", {})
                if isinstance(e, TimeoutError):
                    # too-slow model → let the router swap to a faster fallback
                    # and retry the same step, instead of ending the task.
                    if (self.router and self.router.trigger_on("timeout")
                            and self._try_switch("timeout", self.need, transcript)):
                        continue
                    msg = (f"model timed out ({self.gateway.ref}): {e}. It's slow "
                           f"for interactive use — raise its budget with /timeout, "
                           f"pick a faster model with /model, or /router on to "
                           f"auto-switch.")
                else:
                    # A hard call failure that survived the retries (server
                    # crashed, model can't reload, auth/network) is the
                    # strongest failure signature of all — a dead model must
                    # hand off like a slow one (qwen's llama-server OOM-died
                    # mid-run and the whole task ended one step short).
                    if (self.router and self.router.trigger_on("error")
                            and self._try_switch("error", self.need, transcript)):
                        continue
                    msg = f"model call failed ({self.gateway.ref}): {type(e).__name__}: {str(e)[:200]}"
                self.on_event("error", {"message": msg})
                if self.router is not None:
                    self._record_telemetry(reported_done)  # capture the failure too
                out = {"text": msg, "steps": step, "error": True,
                       "models_used": self._models_used, "switches": self._switches,
                       "jobs": [r.id for r in self.ctx.task_jobs]}
                if isinstance(e, TimeoutError):
                    # let the REPL offer a bigger-budget retry (interactive)
                    out.update(timed_out=True, ref=self.gateway.ref,
                               timeout=getattr(self.gateway, "timeout", None))
                return out
            call_retries += getattr(resp, "_retries", 0)
            # the usage rides on the event so a live status line can count the
            # task's tokens exactly, rather than estimating from characters
            self.on_event("thinking_done", {"input_tokens": resp.input_tokens,
                                            "output_tokens": resp.output_tokens})
            self.ctx.ledger.record_tokens(resp.input_tokens + resp.output_tokens)
            if resp.stop_reason == "length":
                length_stops += 1
            if getattr(resp, "reasoning", ""):
                last_reasoning = resp.reasoning
            if not resp.tool_calls:
                # The server-side template parser can miss a model's tool-call
                # syntax (variant tags, calls emitted in the think channel,
                # truncated closers) — the call then arrives as plain text.
                # Recover it instead of concluding "no tool call".
                from .toolcalls import recover_tool_calls, strip_tool_call_text
                recovered = recover_tool_calls(
                    resp.text, getattr(resp, "reasoning", ""), known_names)
                if recovered:
                    recovered_calls += len(recovered)
                    resp.tool_calls = recovered
                    resp.text = strip_tool_call_text(resp.text)
                    self.on_event("tool_calls_recovered",
                                  {"count": len(recovered),
                                   "names": [c["name"] for c in recovered]})
            if resp.text:
                final_text = resp.text
                self.on_event("assistant", {"text": resp.text})
            transcript.append({"role": "assistant", "content": resp.text,
                               "tool_calls": resp.tool_calls})
            if not resp.tool_calls:
                truncated = resp.stop_reason == "length"
                # A bare refusal ("I can't continue with this task") is not a
                # final answer — swap to a model that will do the work.
                if (self.router and self.router.trigger_on("refusal")
                        and self.router.is_refusal(resp.text)
                        and self._try_switch("refusal", self.need, transcript)):
                    continue
                if resp.text and not truncated:
                    # A non-truncated text-only turn LOOKS like a final answer.
                    # But a weak agentic model routinely NARRATES its plan /
                    # next-steps — or even fabricates a completion report
                    # ("lint passes … CHIPCHAMP_PASS") — as prose instead of
                    # acting or calling report.done, and we would accept that
                    # as the answer: ending prematurely, or worse, on false
                    # success. Unless the completion gate was actually
                    # satisfied (report.done accepted), nudge ONCE to force a
                    # real action or a gate-validated report.done. The counter
                    # resets whenever the model does act, so every narration
                    # that is followed by progress is rescued; two text-only
                    # turns in a row are accepted as the genuine final answer.
                    # Only once the model has done MUTATING work (wrote a file
                    # or ran a job) — that is the task class that must finish
                    # through report.done. A read-only query/investigation
                    # ("describe X" → design.module → prose answer) is a
                    # genuine final answer and must end cleanly, no spurious
                    # nudge.
                    # "Engaged" with a build = made a mutating call OR declared
                    # a plan that still has unfinished steps. A model that
                    # plans, narrates and stops (gpt-oss's signature collapse)
                    # must get the nudge/switch treatment, not read as a
                    # genuine final answer — while a read-only Q&A reply
                    # (no plan, no writes) still ends cleanly.
                    plan_pending = any(s.get("status") != "done"
                                       for s in (self.ctx.plan or []))
                    engaged = made_mutating_call or plan_pending
                    if (engaged and not reported_done
                            and narration_nudges < 1):
                        narration_nudges += 1
                        transcript.append({"role": "user", "content": (
                            "You ended with a message but no tool call, and "
                            "report.done has not accepted this task. If you are "
                            "genuinely finished, call report.done now — it is "
                            "the ONLY way to complete and it validates your work "
                            "against the required gates against real job "
                            "records. Otherwise take the NEXT concrete action as "
                            "a tool call. Do NOT describe what you will do, and "
                            "do NOT claim success in prose — act.")})
                        if self.session:
                            self.session.messages = transcript
                            self.session.save()
                        continue
                    # A build task that narrated twice without finishing: before
                    # accepting it, let the router hand off to another model.
                    if (engaged and not reported_done
                            and self._try_switch("giveup", self.need, transcript)):
                        narration_nudges = 0
                        continue
                    break  # a genuine, complete final answer
                # Otherwise the turn produced no actionable output: it was
                # either empty, or CUT OFF at the token ceiling
                # (finish_reason=length) — a reasoning model can emit a scrap
                # of text ("let me rewrite this…") and then burn the rest of
                # the budget planning, never reaching its tool call. A
                # truncated turn is NOT a final answer: nudge it to continue,
                # bounded, then fail loudly instead of ending silently.
                empty_turns += 1
                if empty_turns > 2:
                    # exhausted the continue-nudges → try another model before
                    # giving up on the task entirely.
                    if self._try_switch("giveup", self.need, transcript):
                        empty_turns = 0
                        continue
                    if last_reasoning and not final_text:
                        # The answer often exists but stayed trapped in the
                        # reasoning channel — surface it, clearly labeled.
                        final_text = ("(recovered from the model's reasoning "
                                      "channel — it never emitted a final "
                                      "message)\n" + last_reasoning[-2000:])
                    else:
                        note = ("(stopped: the model kept hitting the token "
                                "limit before finishing"
                                + (" — raise [model] max_tokens or pick a less "
                                   "verbose model" if truncated else "") + ")")
                        # Keep any partial text, but ALWAYS surface the failure
                        # — never let a truncated fragment pose as the answer.
                        final_text = (final_text + "\n\n" + note
                                      if final_text else note)
                    break
                if truncated:
                    nudge = ("Your previous message was cut off at the token "
                             "limit (finish_reason=length) before you finished."
                             " Continue from where you stopped — if you were "
                             "about to call a tool, emit that tool call now. "
                             "Be concise; do not restart or re-plan from "
                             "scratch.")
                else:
                    nudge = ("Your previous reply was empty. Respond now with "
                             "either a tool call or a concise final answer as "
                             "plain message text. No further internal "
                             "deliberation. /no_think")
                transcript.append({"role": "user", "content": nudge})
                if self.session:
                    self.session.messages = transcript
                    self.session.save()
                continue
            narration_nudges = 0  # the model acted — reset the narration guard
            results = []
            lint_err_this = None   # this step's lint error count (if any)
            job_failed = job_passed = False
            sim_fail_sig = None    # signature of a sim failure seen this step
            for call in resp.tool_calls:
                real = self._name_map.get(call["name"], call["name"])
                tool_obj = self.tools.get(real)
                self.on_event("tool_call", {"name": real, "input": call["input"]})
                result = self._exec(real, call["input"])
                ok = isinstance(result, dict) and not result.get("denied") \
                    and not result.get("error")
                if ok and getattr(tool_obj, "permission", "") in ("write", "submit"):
                    made_mutating_call = True  # real state change → needs report.done
                if real == "report.done" and ok and result.get("accepted"):
                    reported_done = True  # gates satisfied — a later text turn
                    #                       is now a legitimate final answer
                if real == "lint.run" and isinstance(result, dict) \
                        and isinstance(result.get("errors"), int):
                    lint_err_this = result["errors"]
                if real in ("sim.run", "synth.run", "fpga.run") \
                        and isinstance(result, dict):
                    st = result.get("status")
                    if st == "passed" or result.get("sim_status") == "pass":
                        job_passed = True
                    elif st in ("failed", "error"):
                        job_failed = True
                        if real == "sim.run":
                            sim_fail_sig = _sim_failure_signature(
                                call["input"], result)
                results.append({"id": call["id"], "name": real,
                                "output": _stringify(result)})
                self.on_event("tool_result", {"name": real, "result": result})
            transcript.append({"role": "tool", "content": results})
            if self.session:
                self.session.messages = transcript
                self.session.task_frame["plan"] = list(self.ctx.plan)
                self.session.save()
            # STALL: repeated failing checks with NO progress (lint errors not
            # dropping, sims still failing) → the current model can't debug this;
            # hand off to the best debugger. Productive iteration (error count
            # falling, or any pass) resets the counter, so a model grinding lint
            # 8→6→2→0 is never penalised.
            improved = bool(job_passed) or lint_err_this == 0 or (
                lint_err_this is not None and last_lint_err is not None
                and lint_err_this < last_lint_err)
            if lint_err_this is not None:
                last_lint_err = lint_err_this
            if improved:
                stall = 0
            elif job_failed or (lint_err_this is not None and lint_err_this > 0):
                stall += 1
            # BAD-ORACLE GUARD: the same sim failing with the IDENTICAL
            # signature over and over, across RTL edits, is the classic
            # symptom of a broken testbench oracle (e.g. a reference that
            # gates valid on ready, or a per-cycle compare against an elastic
            # DUT) — no RTL fix can ever satisfy it, and a model (or a whole
            # relay of models) will grind sim-edit-sim forever. Detect the
            # unchanging signature and inject a one-time nudge redirecting
            # suspicion to the harness. The nudge lives in the transcript, so
            # a later stall-switch hands the fallback model the same warning.
            if sim_fail_sig is not None:
                if sim_fail_sig == sim_sig:
                    sim_sig_count += 1
                else:
                    sim_sig, sim_sig_count = sim_fail_sig, 1
            elif job_passed:
                sim_sig, sim_sig_count = None, 0
            if (sim_sig is not None and sim_sig_count >= 3
                    and sim_sig not in sim_sigs_nudged):
                sim_sigs_nudged.add(sim_sig)
                self.on_event("oracle_suspect", {
                    "test": sim_sig[0], "signature": sim_sig[1],
                    "count": sim_sig_count})
                transcript.append({"role": "user", "content": (
                    f"The sim '{sim_sig[0]}' has now failed {sim_sig_count} "
                    "consecutive times with the IDENTICAL failure signature "
                    f"({sim_sig[1]!r}). When the signature never changes no "
                    "matter what you fix, the TESTBENCH ORACLE is the prime "
                    "suspect — re-read the harness before touching the RTL "
                    "again, and check it against these laws: (1) a reference "
                    "model's valid must NEVER depend on ready — gating "
                    "expected valid on m_ready is a handshake modelling "
                    "error; (2) elastic / latency-insensitive components "
                    "(skid buffers, elastic pipelines, FIFOs) must be checked "
                    "by STREAM EQUALITY (every beat accepted at an input "
                    "handshake appears at the output, in order, sampled at "
                    "the DUT's OWN handshakes), never by per-cycle "
                    "valid/data comparison against a hand-rolled "
                    "fixed-latency model. If the oracle violates either law, "
                    "fix the HARNESS, not the RTL.")})
                if self.session:
                    self.session.messages = transcript
                    self.session.save()
            if (self.router and self.router.trigger_on("stall") and stall >= 3
                    and self._try_switch("stall", "debugger", transcript)):
                stall = 0
                last_lint_err = None
        if self.router is not None:
            self._record_telemetry(reported_done)
        # Park on anything still running (FR-JOB-04). The session records it so
        # a later `chipchamp wake` can collect the verdict and carry on — the
        # half of detachment that makes it more than fire-and-forget. Note that
        # report.done cannot have accepted on these: a `running` record is not
        # a passing gate, so the structure already refuses to call them done.
        if self.ctx.pending_jobs and self.session:
            self.session.task_frame["pending_jobs"] = list(self.ctx.pending_jobs)
            self.session.task_frame["parked_task"] = user_prompt
            self.session.save()
        return {"text": final_text, "steps": step + 1,
                "pending_jobs": list(self.ctx.pending_jobs),
                "tokens": self.ctx.ledger.tokens_used,
                "model": self.gateway.ref,
                "models_used": self._models_used, "switches": self._switches,
                "empty_turns": empty_turns, "length_stops": length_stops,
                "recovered_calls": recovered_calls,
                "call_retries": call_retries,
                "jobs": [r.id for r in self.ctx.task_jobs]}

    def _record_telemetry(self, completed: bool) -> None:
        """Append a per-run outcome for the telemetry layer (no-op unless on)."""
        tclass = "?"
        try:
            from ..policy.classify import classify
            tclass = classify(self.ctx.current_diff())
        except Exception:
            pass
        self.router.record_outcome(self.ctx.ws.dot, {
            "task_class": tclass,
            "models_used": self._models_used,
            "switches": self._switches,
            "completed": bool(completed),
            "refused": any(s["reason"] == "refusal" for s in self._switches),
            "timed_out": any(s["reason"] == "timeout" for s in self._switches),
            "jobs": len(self.ctx.task_jobs)})

    _SWITCH_HINT = {
        "refusal": "refused the task",
        "timeout": "was too slow (timed out)",
        "stall": "could not make progress (repeated failing checks)",
        "giveup": "stopped without finishing the task",
        "error": "became unreachable (server/model failure)",
    }

    def _try_switch(self, reason: str, need: str, transcript: list) -> bool:
        """Hot-swap `self.gateway` to the next fallback model for `need`,
        preserving the transcript. Returns True (caller should `continue`) if a
        switch happened; False (no-op) when the router is disabled, this trigger
        is off, or the pool is exhausted. Each pool model is tried at most once
        per run, so switching can't loop."""
        r = self.router
        if not (r and r.cfg.enabled and r.cfg.layers.get("reactive", True)
                and r.cfg.triggers.get(reason, True)):
            return False
        cur = self.gateway.ref
        self._tried.add(cur)
        gw = nxt = None
        while True:
            nxt = r.next_fallback(cur, need, self._tried)
            if not nxt:
                return False
            gw = r.build_gateway(nxt, like=self.gateway)
            if gw is not None and gw.available:
                break
            self._tried.add(nxt)  # misconfigured / unreachable — skip it
        self.gateway = gw
        self._tried.add(nxt)
        self._switches.append({"from": cur, "to": nxt, "reason": reason})
        self._models_used.append(nxt)
        self.on_event("model_switch", {"from": cur, "to": nxt, "reason": reason})
        note = (
            f"[router] You ({nxt}) have taken over because the previous model "
            f"({cur}) {self._SWITCH_HINT.get(reason, reason)}. Review the "
            f"transcript so far and continue the task from where it stands — "
            f"take the next concrete action (a tool call), or call report.done "
            f"ONLY if the required gates already pass. Do not restart from "
            f"scratch and do not claim success in prose.")
        if transcript and transcript[-1].get("role") == "tool":
            # A user turn directly after tool results breaks strict chat
            # templates (Mistral: "conversation roles must alternate user and
            # assistant ... except for tool calls and results" — the exact
            # error a stall handoff to devstral produced). Carry the handoff
            # in the system prompt instead of the conversation.
            self._handoff_note = note
        else:
            transcript.append({"role": "user", "content": note})
        if self.session:
            self.session.messages = transcript
            self.session.save()
        return True

    _RETRYABLE = ("HTTP 5", "HTTP 400", "HTTP 429", "peg-native", "format",
                  "Connection", "RemoteDisconnected", "IncompleteRead",
                  "JSONDecodeError")
    _NOT_RETRYABLE = ("HTTP 401", "HTTP 403", "HTTP 404")

    def _call_with_retry(self, system: str, transcript: list[dict],
                         attempts: int = 3) -> ModelResponse:
        """A single bad generation must not kill a whole task. Local engines
        occasionally 500 mid-stream (grammar-constrained decoders rejecting a
        malformed sample — LM Studio's 'peg-native format' error is one
        sampled glitch, not a broken endpoint); those are retryable. Auth and
        not-found errors are not. Timeouts are not retried — the model being
        too slow is a fact, not a glitch."""
        import time as _time
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._call(system, transcript)
                if attempt:
                    resp._retries = attempt
                return resp
            except TimeoutError:
                raise
            except Exception as e:
                s = str(e)
                if any(k in s for k in self._NOT_RETRYABLE) or \
                        not any(k in s for k in self._RETRYABLE):
                    raise
                last = e
                self.on_event("error", {"message": f"model call glitched "
                              f"(attempt {attempt + 1}/{attempts}), retrying: "
                              f"{s[:120]}"})
                _time.sleep(1.5 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _call(self, system: str, transcript: list[dict]) -> ModelResponse:
        audit = self._audit_items(transcript[-1])
        # A router handoff that fired right after tool results rides in the
        # system prompt (see _try_switch) — the conversation stays template-legal.
        if getattr(self, "_handoff_note", ""):
            system = f"{system}\n\n{self._handoff_note}"
        kw = {"audit_items": audit}
        # Stream tokens to the UI as "delta" events when this gateway can.
        # Signature-gated so router hot-swaps to a non-streaming gateway (and
        # test fakes with the pre-streaming signature) keep working untouched.
        # NOTE: deltas fire from the gateway's deadline thread — on_event is
        # crash-shielded and the CLI renderers are thread-safe by design.
        import inspect
        try:
            if "on_delta" in inspect.signature(self.gateway.complete).parameters:
                kw["on_delta"] = lambda ch, chunk: self.on_event(
                    "delta", {"channel": ch, "chunk": chunk})
        except (TypeError, ValueError):
            pass
        return self.gateway.complete(system, transcript, self._api_tools, **kw)

    def _exec(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
        if not tool:
            return {"error": f"unknown tool {name}"}
        # destructive ops (delete/overwrite) ALWAYS require explicit human
        # confirmation, independent of autonomy mode (never auto-approved). A
        # destructive tool that clears this confirm is NOT then gated again by
        # plan mode (that would double-prompt for one action) — hence elif.
        gated = name not in self._GATE_EXCLUDE
        if gated and getattr(tool, "destructive", False):
            self.on_event("destructive", {"name": name, "input": args or {}})
            if not self.approver(name, args or {}, "destructive"):
                return {"denied": True, "tool": name,
                        "note": "destructive action not confirmed by the human. "
                                "Do not retry; leave the file in place or ask."}
        # plan mode: gate mutating / job-launching tools on human approval
        elif (gated and self.gate_permissions
                and tool.permission in self.gate_permissions):
            if not self.approver(name, args or {}, tool.permission):
                return {"denied": True, "tool": name,
                        "note": "the human did not approve this action (plan mode). "
                                "Adjust the plan or explain and stop; do not retry it."}
        # Job-launching tools (sim/synth/cov/formal/pnr/fpga/lec/…) block while a
        # real EDA job runs. Announce start/finish so the user sees the subtask
        # running instead of dead air (item: live job visibility).
        is_job = tool.cost in ("metered", "licensed")
        if is_job:
            self.on_event("job_start", {"name": name, "input": args or {}})
        before = len(self.ctx.task_jobs)
        try:
            result = tool.handler(self.ctx, **(args or {}))
        except BudgetExceeded as e:
            # FR-BUDG-01: a ceiling pauses the task and asks the human. The
            # model is told plainly that retrying cannot help, so it reports
            # what it has instead of grinding against a wall.
            self.on_event("budget_block", {"name": name, "reason": str(e)})
            result = {"denied": True, "budget": True, "tool": name,
                      "note": f"{e}. Retrying will not help — no further jobs "
                              f"of this kind can run. Summarize what the "
                              f"evidence so far supports and stop."}
        except TypeError as e:
            result = {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:  # a tool crash must not kill the loop
            result = {"error": f"{type(e).__name__}: {e}"}
        # A reused verdict is real evidence but not fresh work: say so in the
        # result, so neither the model nor a reader of the transcript can
        # mistake a cache hit for a tool that just ran.
        if isinstance(result, dict) and any(
                getattr(r, "cached", False) for r in self.ctx.task_jobs[before:]):
            result["cached"] = True
        if is_job:
            self.on_event("job_done", {"name": name, "result": result})
        # FR-SEC-05: flag instruction-shaped content inside tool output — it is
        # data from the design/logs, never instructions.
        from ..policy.injection import annotate_result, scan_text
        findings = scan_text(_stringify(result))
        if findings:
            result = annotate_result(result, findings)
            self.on_event("security", {"tool": name, "findings": findings})
        return result

    def _audit_items(self, last_turn: dict) -> list[dict]:
        """Provenance of the context items being sent this turn (FR-SEC-03)."""
        items = []
        if last_turn.get("role") == "tool":
            for res in last_turn.get("content", []):
                out = res.get("output", "")
                items.append({"source": f"tool_result:{res.get('name','')}",
                              "bytes": len(out) if isinstance(out, str) else 0})
        elif last_turn.get("role") == "user":
            c = last_turn.get("content", "")
            items.append({"source": "user_prompt",
                          "bytes": len(c) if isinstance(c, str) else 0})
        return items


def transcript_size(transcript: list[dict]) -> dict:
    """`{"chars": n, "messages": m}` for the turn about to be sent.

    Characters, not tokens: the loop is provider-neutral and every tokenizer
    disagrees, but chars are exact, free, and monotone in the thing we care
    about. Divide by ~4 for a rough token count. Tool results dominate the
    total, which is precisely why this is worth watching."""
    chars = 0
    for m in transcript:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):          # tool-result turns
            for r in c:
                if isinstance(r, dict):
                    chars += len(str(r.get("output", "")))
        for call in m.get("tool_calls") or []:
            chars += len(str(call.get("input", "")))
    return {"chars": chars, "messages": len(transcript)}


def _stringify(result) -> str:
    import json
    try:
        return json.dumps(result)[:12000]
    except (TypeError, ValueError):
        return str(result)[:12000]


def _sim_failure_signature(call_input, result) -> tuple[str, str]:
    """Stable identity of a sim failure, for the bad-oracle guard.

    (test name, digit-normalized failure text): numbers are collapsed so an
    RTL edit that merely shifts WHERE the oracle complains ("Cycle 11: m_valid
    mismatch" -> "Cycle 13: ...") still reads as the SAME unchanging
    signature — it is the complaint that matters, not the cycle stamp."""
    import re
    test = ""
    if isinstance(call_input, dict):
        test = str(call_input.get("test", ""))
    text = str(result.get("summary") or "")
    errs = result.get("errors")
    if isinstance(errs, list) and errs and isinstance(errs[0], dict):
        text += " | " + str(errs[0].get("message", ""))
    if not text.strip(" |"):
        text = str(result.get("status", "failed"))
    return (test, re.sub(r"\d+", "N", text)[:300])
