"""The agent loop (SPEC §8.2): plan → act via tools → observe → iterate.

Model-driven control flow over the tool catalog, provider-neutral. The loop keeps
a JSON-safe *neutral transcript* (see :mod:`chipchamp.agent.providers.base`); each
provider translates it to its own wire format, so the same loop drives Anthropic,
OpenAI, or a local server. Tool-result provenance is fed to the context audit log
each turn. With :class:`NullGateway` the loop reports that a scripted playbook
should be used instead.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..tools import all_tools
from ..tools.context import ToolContext
from ..tools.executor import coerce_args, invoke, stringify  # noqa: F401
from . import disclosure, retry, teachings, working_set
from .runstate import LEDGER_TAG, RunState, VerificationTracker, WorkspaceLedger  # noqa: F401
from .gateway import ModelGateway
from .prompts import build_system_prompt, policy_summary
from .providers.base import ModelResponse
from .session import Session, close_dangling_tool_calls


_PLAN_CUE = re.compile(
    r"(?:\b(?:let me|i will|i'll|next,? i|now i(?:'ll| will)|the "
    r"(?:proportional |minimal |correct |right )?fix is|i need to|"
    r"i must)\b[^.\n]{8,240})", re.I)


def _stated_plan(text: str) -> str:
    """The model's own next-step sentence, if its turn contains one."""
    m = _PLAN_CUE.search(text or "")
    return m.group(0).strip() if m else ""


_MODULE_HDR = re.compile(
    r"\bmodule\s+(\w+)\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\(([^;]*?)\)\s*;",
    re.S)


def _port_signatures(text: str) -> dict:
    """{module_name: normalized ANSI port list} for every module in text."""
    out = {}
    for m in _MODULE_HDR.finditer(text or ""):
        out[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()
    return out


def _verify_key(tool: str, inp: dict):
    """(key, scope, rank) for a verification call. Two lints with different
    scopes (file-level vs elaboration with a top) are DIFFERENT checks: a run
    alternated between them on an unchanged tree and the loop called the
    tree 'green' on the weaker pass while the stronger one was red."""
    inp = inp or {}
    parts = []
    for k in sorted(inp):
        v = inp[k]
        parts.append(f"{k}={','.join(map(str, v)) if isinstance(v, list) else v}")
    scope = " ".join(parts) or "default"
    rank = {"lint.run": 0, "sim.run": 2, "cov.run": 2, "lec.run": 2}.get(tool, 1)
    if tool == "lint.run" and inp.get("top"):
        rank = 1
    return (tool, scope), scope, rank


_WRITE_CUE = re.compile(
    r'"?(?:path|file)"?\s*[:=]\s*"([^"\n]{3,120}\.(?:sv|svh|v|vh|cpp|hpp|h|c|py|md|yaml|yml|json))"',
    re.I)


def _truncated_write_target(resp) -> str:
    """The file a cut-off turn was in the middle of writing, if any. A run
    died on three consecutive 32K length-stops trying to emit a whole C++
    harness in one turn; the generic 'continue' nudge just invited the same
    oversized attempt again."""
    blob = (getattr(resp, "text", "") or "")
    tail = (getattr(resp, "reasoning", "") or "")[-4000:]
    for hay in (blob, tail):
        m = _WRITE_CUE.search(hay or "")
        if m:
            return m.group(1)
    # a bare fs.write mention without a parsable path still signals authoring
    if re.search(r"fs[_.]{1,2}write", (blob + tail), re.I):
        return "the file you were writing"
    return ""




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
                 disclose: bool = False, context_budget: int = 0,
                 require_report_done: bool = False,
                 max_narration_nudges: int = 1,
                 verify_every_n_writes: int = 0,
                 progress_guard_after: int = 0):
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
        # separator-free slugs for lenient resolution; only unambiguous ones
        slugs: dict = {}
        for n in self.tools:
            k = re.sub(r"[-_.]", "", n.lower())
            slugs[k] = None if k in slugs else n
        self._slug_map = {k: v for k, v in slugs.items() if v}
        self.disclose = bool(disclose)
        self._loaded = (disclosure.core_names(self.tools) if self.disclose
                        else set(self.tools))
        self._prompt_dirty = False
        # 0 = never compact (the old unbounded behaviour, kept for tests and
        # for anyone who would rather hit a hard wall than lose a body)
        self.context_budget = max(0, int(context_budget or 0))
        # Headless batch runs: a brief that ends in report.done means a bare
        # text turn is NEVER a real answer — treat the task as engaged from
        # step one, and allow more than one consecutive narration nudge (a
        # low-effort model can narrate straight through a single nudge; five
        # tiers of a campaign died at 2-4 steps proving it).
        self.require_report_done = bool(require_report_done)
        self.max_narration_nudges = max(1, int(max_narration_nudges or 1))
        # Verification-cadence guard (0 = off): a fast model can author
        # forever without ever closing a loop — one run wrote 10 files and
        # deleted 3 across 70 steps with ZERO lint jobs. When N writes
        # accumulate with no verification job since the last one, teach the
        # rhythm at the moment of drift, like the narration nudge does.
        self.verify_every_n_writes = max(0, int(verify_every_n_writes or 0))
        # Progress guard (0 = off), the dual of the cadence guard: with green
        # states banked, a model can re-read and re-lint an UNCHANGED green
        # workspace forever ('run a fresh lint to lock ground truth before
        # writing the top' — 67 reads, 10 lints, 8 writes, top never
        # written). After N consecutive non-constructive turns on a green,
        # unchanged state, name the plan's next unfinished step and demand it.
        self.progress_guard_after = max(0, int(progress_guard_after or 0))
        self._port_sigs: dict = {}   # module -> port-list signature last seen
        # Files that arrived via lib.fetch. The compaction pin and the
        # 'this is fetched material' teaching key off PROVENANCE, not off a
        # path prefix: a run wrote seven of its own modules into
        # work/rtl/lib/, judged them corrupt, and thrashed for ~90 steps.
        self.fetched_paths: set = set()
        # A router handoff that fires right after tool results rides in the
        # system prompt for ONE call (see _try_switch / _call_with_retry).
        self._handoff_note = ""
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

    def _persist(self, transcript: list) -> None:
        if self.session:
            self.session.messages = transcript
            self.session.save()

    def _nudge(self, transcript: list, guard: str, text: str) -> None:
        """Inject a synthetic user turn tagged with the guard that wrote it,
        and persist — the seven inlined copies of this are now one."""
        transcript.append({"role": "user", "guard": guard, "content": text})
        self._persist(transcript)

    def run(self, user_prompt: str) -> dict:
        if not self.gateway.available:
            return {"text": "no model configured; run `chipchamp model` to pick one, "
                    "or use a scripted playbook (chipchamp triage / lint)", "steps": 0}
        transcript: list[dict] = list(self.session.messages) if self.session else []
        # A session saved mid-interrupt (older builds) may end on unanswered
        # tool calls; repair before the provider ever sees it.
        if close_dangling_tool_calls(transcript):
            self.on_event("info", {"message": "repaired unanswered tool calls "
                                              "left by an interrupted turn"})
        # `input: True` marks a GENUINE user turn — the loop also injects
        # synthetic "user" nudges/observations (each tagged `guard`), so a
        # resumed session tells real requests apart. Providers read only
        # role/content/tool_calls, so the extra keys are inert.
        transcript.append({"role": "user", "content": user_prompt, "input": True})
        system = self.system_prompt(task=user_prompt)
        self._warn_if_window_too_small(system)
        st = RunState()
        led = WorkspaceLedger()
        ver = VerificationTracker()
        known_names = set(self._name_map) | set(self._name_map.values())
        self._tried = set()          # model refs already used/skipped this run
        self._handoff_note = ""      # a stale note must not outlive its run
        self._switches = []          # [{from, to, reason}] for the return dict
        self._models_used = [self.gateway.ref]
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
        for st.step in range(self.max_steps):
            # the token ceiling is checked HERE, not advertised in the prompt:
            # telling a model its budget is not a budget (§12.4 FR-BUDG-01)
            ok, why = self.ctx.ledger.can_spend_tokens()
            if not ok:
                self.on_event("budget_block", {"name": "model", "reason": why})
                # A whole campaign misread this exit as 'died narrating':
                # the model's last prose stood in for the reason. Keep the
                # prose, but make the stop itself unmistakable in the result.
                st.budget_stop = why
                st.final_text = (st.final_text + "\n\n" if st.final_text else "") + \
                    f"(stopped: model token budget exhausted — {why})"
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
                    transcript, budget_chars=self.context_budget,
                    pinned=self.fetched_paths)
                if moved["compacted"]:
                    self.on_event("compacted", moved)
                    led.active = True
                if led.active:
                    # Pinned workspace inventory: after 21 compactions a
                    # model claimed 'top module written, lint at 0 errors'
                    # over a tree holding two files — it trusted evicted
                    # memory over the filesystem. The loop's own ledger is
                    # authoritative; keep exactly one copy, refreshed at
                    # every compaction (user turns are never evicted).
                    if led.dirty:
                        led.refresh(transcript, ver.summary(led.epoch))
                    else:
                        led.strip(transcript)
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
                    self._record_telemetry(st.reported_done)  # capture the failure too
                out = {"text": msg, "steps": st.step, "error": True,
                       "models_used": self._models_used, "switches": self._switches,
                       "jobs": [r.id for r in self.ctx.task_jobs]}
                if isinstance(e, TimeoutError):
                    # let the REPL offer a bigger-budget retry (interactive)
                    out.update(timed_out=True, ref=self.gateway.ref,
                               timeout=getattr(self.gateway, "timeout", None))
                return out
            st.call_retries += getattr(resp, "_retries", 0)
            # the usage rides on the event so a live status line can count the
            # task's tokens exactly, rather than estimating from characters
            self.on_event("thinking_done", {"input_tokens": resp.input_tokens,
                                            "output_tokens": resp.output_tokens})
            self.ctx.ledger.record_tokens(resp.input_tokens + resp.output_tokens)
            if resp.stop_reason == "length":
                st.length_stops += 1
            if getattr(resp, "reasoning", ""):
                st.last_reasoning = resp.reasoning
            if not resp.tool_calls:
                # The server-side template parser can miss a model's tool-call
                # syntax (variant tags, calls emitted in the think channel,
                # truncated closers) — the call then arrives as plain text.
                # Recover it instead of concluding "no tool call".
                from .toolcalls import recover_tool_calls, strip_tool_call_text
                recovered = recover_tool_calls(
                    resp.text, getattr(resp, "reasoning", ""), known_names)
                if recovered:
                    st.recovered_calls += len(recovered)
                    resp.tool_calls = recovered
                    resp.text = strip_tool_call_text(resp.text)
                    self.on_event("tool_calls_recovered",
                                  {"count": len(recovered),
                                   "names": [c["name"] for c in recovered]})
            if resp.text:
                st.final_text = resp.text
                self.on_event("assistant", {"text": resp.text})
            transcript.append({"role": "assistant", "content": resp.text,
                               "tool_calls": resp.tool_calls})
            if not resp.tool_calls:
                if self._handle_text_only_turn(st, resp, transcript):
                    continue
                break  # a genuine, complete final answer, or an explicit stop
            st.narration_nudges = 0  # the model acted — reset the narration guard
            results: list[dict] = []
            pending_teach: list[tuple[str, str]] = []
            lint_err_this = None   # this step's lint error count (if any)
            job_failed = job_passed = False
            sim_fail_sig = None    # signature of a sim failure seen this step
            for call in resp.tool_calls:
                real = self._resolve_tool_name(call["name"])
                tool_obj = self.tools.get(real)
                inp = call.get("input") or {}
                path = str(inp.get("path", ""))
                self.on_event("tool_call", {"name": real, "input": inp})
                try:
                    result = self._exec(real, inp)
                except KeyboardInterrupt:
                    # Ctrl-C mid-tool: answer this call and every call after
                    # it, persist, THEN propagate — the saved transcript must
                    # stay legal or the whole session is dead.
                    done = {r["id"] for r in results}
                    for tc in resp.tool_calls:
                        if tc["id"] not in done:
                            results.append({"id": tc["id"], "name": tc["name"],
                                            "output": '{"error": "interrupted '
                                                      'by user — this call never '
                                                      'ran"}'})
                    transcript.append({"role": "tool", "content": results})
                    self._persist(transcript)
                    raise
                ok = isinstance(result, dict) and not result.get("denied") \
                    and not result.get("error")
                if ok and getattr(tool_obj, "permission", "") in ("write", "submit"):
                    st.made_mutating_call = True  # real state change → needs report.done
                if ok and real == "lib.fetch" and isinstance(result, dict):
                    for _f in (list(result.get("written") or [])
                               + list(result.get("unchanged") or [])):
                        self.fetched_paths.add(str(_f))
                if ok and real in ("fs.write", "fs.edit"):
                    st.writes_since_verify += 1
                    if (path and path not in self.fetched_paths
                            and "/lib/" in "/" + path
                            and (path, "libdir") not in ver.scope_taught):
                        ver.scope_taught.add((path, "libdir"))
                        pending_teach.append(("libdir", teachings.libdir(path)))
                    _teach = self._port_change_teaching(path)
                    if _teach:
                        pending_teach.append(("ports", _teach))
                    led.wrote(path)
                if ok and real in ("fs.write", "fs.edit", "fs.delete",
                                   "checkpoint.restore"):
                    led.bump()
                if ok and real == "fs.delete" and path:
                    led.removed(path)
                    # A run deleted the very RTL its green checkpoint held;
                    # no teaching fired because the restore path only watched
                    # for FAILING checks. A wipe loses a good state just as well.
                    if (ver.green_job and path in self._checkpoint_files(ver.green_job)
                            and not ver.checkpoint_taught):
                        ver.checkpoint_taught = True
                        pending_teach.append(("wipe", teachings.wipe(path, ver.green_job)))
                if real in ("lint.run", "sim.run", "cov.run", "lec.run"):
                    st.writes_since_verify = 0   # ran a check (pass or fail):
                    st.verify_nudged_at = 0      # the loop is closing loops
                if real == "report.done" and ok and result.get("accepted"):
                    st.reported_done = True  # gates satisfied — a later text turn
                    #                          is now a legitimate final answer
                if real == "lint.run" and isinstance(result, dict) \
                        and isinstance(result.get("errors"), int):
                    lint_err_this = result["errors"]
                if real in ("lint.run", "sim.run") and isinstance(result, dict):
                    key, scope, rank = _verify_key(real, inp)
                    pending_teach += ver.observe(
                        real, key, scope, rank, result, led.epoch,
                        on_green=lambda job: self._save_green_checkpoint(job, led.authored))
                if real in ("sim.run", "synth.run", "fpga.run") \
                        and isinstance(result, dict):
                    _st = result.get("status")
                    if _st == "passed" or result.get("sim_status") == "pass":
                        job_passed = True
                    elif _st in ("failed", "error"):
                        job_failed = True
                        if real == "sim.run":
                            sim_fail_sig = _sim_failure_signature(inp, result)
                results.append({"id": call["id"], "name": real,
                                "output": _stringify(result)})
                self.on_event("tool_result", {"name": real, "result": result})
            transcript.append({"role": "tool", "content": results})
            for tag, msg in pending_teach:
                transcript.append({"role": "user", "guard": tag, "content": msg})
            if led.active and led.dirty:
                # refresh NOW, not only at the next compaction: a guard
                # message appended this step must never sit next to a
                # ledger that still describes the previous step
                led.refresh(transcript, ver.summary(led.epoch))
            if (self.verify_every_n_writes
                    and st.writes_since_verify >= self.verify_every_n_writes
                    and st.writes_since_verify >= st.verify_nudged_at
                            + self.verify_every_n_writes):
                # RE-ARMS every N further writes: latching once per breach let
                # a run author 37 files against 6 checks — the teaching went
                # quiet exactly when the drift got worse
                st.verify_nudged_at = st.writes_since_verify
                transcript.append({"role": "user", "guard": "verify_cadence",
                                   "content": teachings.verify_cadence(st.writes_since_verify)})
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
                lint_err_this is not None and st.last_lint_err is not None
                and lint_err_this < st.last_lint_err)
            # A model rewriting whole files rarely NOTICES it is regressing —
            # one run drove a lint queue 3->9->19, each "fix" breaking more.
            # State the scoreboard at the moment of drift and demand the
            # minimal-edit protocol, exactly like the narration nudge does.
            if (lint_err_this is not None and st.last_lint_err is not None
                    and lint_err_this > st.last_lint_err):
                self._nudge(transcript, "lint_rose",
                            teachings.lint_rose(st.last_lint_err, lint_err_this))
            if lint_err_this is not None:
                st.last_lint_err = lint_err_this
            constructive = any(
                self._resolve_tool_name(c["name"]) in (
                    "fs.write", "fs.edit", "fs.delete", "lib.fetch",
                    "checkpoint.restore", "report.done")
                for c in resp.tool_calls)
            if constructive:
                st.idle_turns = 0
                st.progress_taught = False
            else:
                st.idle_turns += 1
            if (self.progress_guard_after and ver.is_green(led.epoch)
                    and st.idle_turns >= self.progress_guard_after
                    and not st.progress_taught):
                st.progress_taught = True
                nxt = next((str(p.get("step", "")) for p in (self.ctx.plan or [])
                            if p.get("status") != "done"), "")
                self._nudge(transcript, "progress",
                            teachings.progress(ver.green_job, ver.summary(led.epoch), nxt))
            # Post-green regression tangle: a run went green at J-0004, then
            # revised itself into five straight failing lints and said "my
            # files have drifted into a tangle... I can't see the actual
            # on-disk wiring". Green states are checkpointed — teach the way
            # back instead of watching the tangle grow.
            if (ver.green_job and ver.fails_since_green >= 2
                    and not ver.checkpoint_taught):
                ver.checkpoint_taught = True
                self._nudge(transcript, "checkpoint",
                            teachings.checkpoint(ver.fails_since_green, ver.green_job))
            if improved:
                st.stall = 0
            elif job_failed or (lint_err_this is not None and lint_err_this > 0):
                st.stall += 1
            # BAD-ORACLE GUARD: the same sim failing with the IDENTICAL
            # signature over and over, across RTL edits, is the classic
            # symptom of a broken testbench oracle — no RTL fix can ever
            # satisfy it, and a model (or a whole relay of models) will grind
            # sim-edit-sim forever. Detect the unchanging signature and
            # redirect suspicion to the harness, once. The nudge lives in the
            # transcript, so a later stall-switch hands the fallback model
            # the same warning.
            st.saw_sim_signature(sim_fail_sig, job_passed)
            if st.oracle_suspect():
                st.sim_sigs_nudged.add(st.sim_sig)
                self.on_event("oracle_suspect", {
                    "test": st.sim_sig[0], "signature": st.sim_sig[1],
                    "count": st.sim_sig_count})
                self._nudge(transcript, "oracle",
                            teachings.oracle(st.sim_sig[0], st.sim_sig[1], st.sim_sig_count))
            if (self.router and self.router.trigger_on("stall") and st.stall >= 3
                    and self._try_switch("stall", "debugger", transcript)):
                st.stall = 0
                st.last_lint_err = None
        if self.router is not None:
            self._record_telemetry(st.reported_done)
        # Park on anything still running (FR-JOB-04). The session records it so
        # a later `chipchamp wake` can collect the verdict and carry on — the
        # half of detachment that makes it more than fire-and-forget. Note that
        # report.done cannot have accepted on these: a `running` record is not
        # a passing gate, so the structure already refuses to call them done.
        if self.ctx.pending_jobs and self.session:
            self.session.task_frame["pending_jobs"] = list(self.ctx.pending_jobs)
            self.session.task_frame["parked_task"] = user_prompt
            self.session.save()
        return {"text": st.final_text, "steps": st.step + 1,
                "budget_stop": st.budget_stop,
                "pending_jobs": list(self.ctx.pending_jobs),
                "tokens": self.ctx.ledger.tokens_used,
                "model": self.gateway.ref,
                "models_used": self._models_used, "switches": self._switches,
                "empty_turns": st.empty_turns, "length_stops": st.length_stops,
                "recovered_calls": st.recovered_calls,
                "call_retries": st.call_retries,
                "jobs": [r.id for r in self.ctx.task_jobs]}

    def _handle_text_only_turn(self, st: RunState, resp, transcript: list) -> bool:
        """A turn with no tool call. Returns True to take another step
        (a nudge or a model switch was issued), False to end the run —
        either on a genuine final answer or an explicit, loud stop."""
        truncated = resp.stop_reason == "length"
        # A bare refusal ("I can't continue with this task") is not a final
        # answer — swap to a model that will do the work.
        if (self.router and self.router.trigger_on("refusal")
                and self.router.is_refusal(resp.text)
                and self._try_switch("refusal", self.need, transcript)):
            return True
        if resp.text and not truncated:
            # A non-truncated text-only turn LOOKS like a final answer. But a
            # weak agentic model routinely NARRATES its plan — or fabricates a
            # completion report — as prose instead of acting or calling
            # report.done. Unless the completion gate was actually satisfied,
            # nudge (bounded) to force a real action or a gate-validated
            # report.done; the counter resets whenever the model acts.
            # "Engaged" = made a mutating call OR declared a plan that still
            # has unfinished steps OR this run must end on report.done. A
            # read-only Q&A reply (no plan, no writes) ends cleanly.
            plan_pending = any(s.get("status") != "done"
                               for s in (self.ctx.plan or []))
            engaged = (st.made_mutating_call or plan_pending
                       or self.require_report_done)
            if (engaged and not st.reported_done
                    and st.narration_nudges < self.max_narration_nudges):
                st.narration_nudges += 1
                # Quote the model's own plan back as the directive — the
                # knowledge is there; only the act is missing.
                self._nudge(transcript, "narration",
                            teachings.narration(_stated_plan(resp.text or "")))
                return True
            # A build task that narrated past the nudges without finishing:
            # before accepting it, let the router hand off to another model.
            if (engaged and not st.reported_done
                    and self._try_switch("giveup", self.need, transcript)):
                st.narration_nudges = 0
                return True
            return False  # a genuine, complete final answer
        # Otherwise the turn produced no actionable output: it was either
        # empty, or CUT OFF at the token ceiling (finish_reason=length) — a
        # reasoning model can emit a scrap of text and then burn the rest of
        # the budget planning, never reaching its tool call. A truncated turn
        # is NOT a final answer: nudge it to continue, bounded, then fail
        # loudly instead of ending silently.
        st.empty_turns += 1
        if st.empty_turns > 2:
            # exhausted the continue-nudges → try another model before
            # giving up on the task entirely.
            if self._try_switch("giveup", self.need, transcript):
                st.empty_turns = 0
                return True
            if st.last_reasoning and not st.final_text:
                # The answer often exists but stayed trapped in the reasoning
                # channel — surface it, clearly labeled.
                st.final_text = ("(recovered from the model's reasoning "
                                 "channel — it never emitted a final "
                                 "message)\n" + st.last_reasoning[-2000:])
                # synthesized here, not model-emitted: fire the event so every
                # surface renders it exactly once
                self.on_event("assistant", {"text": st.final_text, "synthetic": True})
            else:
                note = ("(stopped: the model kept hitting the token limit "
                        "before finishing"
                        + (" — raise [model] max_tokens or pick a less "
                           "verbose model" if truncated else "") + ")")
                # Keep any partial text, but ALWAYS surface the failure —
                # never let a truncated fragment pose as the answer.
                st.final_text = (st.final_text + "\n\n" + note
                                 if st.final_text else note)
                self.on_event("assistant", {"text": note, "synthetic": True})
            return False
        if truncated:
            target = _truncated_write_target(resp)
            if target or st.length_stops >= 2:
                nudge = teachings.chunked_write(target, st.length_stops)
            else:
                nudge = teachings.continue_after_cutoff()
        else:
            nudge = teachings.empty_turn()
        self._nudge(transcript, "continue", nudge)
        return True

    def _warn_if_window_too_small(self, system: str) -> None:
        """Say so when the prompt cannot fit the model's declared window.

        A local server defaults to a small context (ollama: 4096 tokens) while
        chipchamp's system prompt plus tool schemas is several times that. The
        symptom is not an error — the request is accepted and the server grinds,
        so the run looks like a hang and then times out having taken zero steps.
        That cost a whole agentic demo run to diagnose, and the number was
        knowable before the first token was sent.
        """
        try:
            num_ctx = int((getattr(self.gateway, "options", None) or {})
                          .get("num_ctx") or 0)
        except (TypeError, ValueError, AttributeError):
            return
        if num_ctx <= 0:
            return          # no declared window: nothing to compare against
        import json as _json
        tools = sum(len(_json.dumps(t.get("schema", {})))
                    + len(t.get("description", "")) for t in self._api_tools)
        need = (len(system) + tools) // 4     # ~4 chars/token, provider-neutral
        if need <= num_ctx * 0.8:             # leave room for the conversation
            return
        hint = ("" if self.disclose else
                "enable [model] tool_disclosure to defer most schemas, or ")
        self.on_event("error", {"message": (
            f"context too small: system prompt + tool schemas are ~{need} "
            f"tokens but this model's num_ctx is {num_ctx}. The server accepts "
            f"the request and stalls rather than refusing it, so this will "
            f"look like a hang. {hint}raise it with "
            f"`/model tune num_ctx={max(8192, need * 2)}`.")})

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
            transcript.append({"role": "user", "guard": "handoff", "content": note})
        if self.session:
            self.session.messages = transcript
            self.session.save()
        return True


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
                # The model has now seen the handoff; a retry of THIS call
                # still carries it (cleared only on success), the next call
                # must not — it was outliving the whole REPL session.
                self._handoff_note = ""
                if attempt:
                    resp._retries = attempt
                return resp
            except TimeoutError:
                raise
            except Exception as e:
                ok, why = retry.classify(e)
                if not ok:
                    raise
                last = e
                self.on_event("error", {"message": f"model call failed "
                              f"({why}; attempt {attempt + 1}/{attempts}), "
                              f"retrying: {str(e)[:120]}"})
                if attempt + 1 < attempts:
                    _time.sleep(retry.backoff(e, attempt))
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

    def _resolve_tool_name(self, name: str) -> str:
        """Resolve a model-emitted tool name to a catalog key, leniently.

        Local models mangle names in ways an exact lookup punishes for no
        gain: a live run emitted `efpga__fabulous.bitstream` — the separator
        convention applied to the wrong separator (the wire form is
        `efpga-fabulous__bitstream`). Names differing only in separators or
        stray whitespace are unambiguous; refusing them converts a cosmetic
        slip into a lost step.
        """
        name = (name or "").strip()
        if name in self.tools:
            return name
        mapped = self._name_map.get(name)
        if mapped:
            return mapped
        # unicode dashes fold to ASCII before the slug comparison
        for d in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014"):
            name = name.replace(d, "-")
        if name in self.tools:
            return name
        slug = re.sub(r"[-_.]", "", name.lower())
        hit = self._slug_map.get(slug)
        return hit if hit else name

    def _port_change_teaching(self, rel: str) -> str:
        """After a module's PORT LIST changes, name every file that
        instantiates it. A run rewrote chan_tagger's ports and left the top's
        instance stale — 8 PINNOTFOUND errors it then narrated about until
        its nudges ran out. The change is mechanical to detect; make it
        mechanical to hear."""
        import os
        if not rel or not rel.endswith((".sv", ".v")):
            return ""
        root = self.ctx.ws.root
        try:
            text = open(os.path.join(root, rel), errors="replace").read()
        except OSError:
            return ""
        changed = []
        for name, sig in _port_signatures(text).items():
            prev = self._port_sigs.get(name)
            self._port_sigs[name] = sig
            if prev is not None and prev != sig:
                changed.append(name)
        if not changed:
            return ""
        hits: dict = {}
        for dirpath, _dirs, files in os.walk(root):
            if "/lib" in dirpath or "/.chipchamp" in dirpath:
                continue
            for fn in files:
                if not fn.endswith((".sv", ".v")):
                    continue
                fp = os.path.join(dirpath, fn)
                frel = os.path.relpath(fp, root)
                if frel == rel:
                    continue
                try:
                    body = open(fp, errors="replace").read()
                except OSError:
                    continue
                for name in changed:
                    if re.search(r"\b" + re.escape(name)
                                 + r"\s+(?:#\s*\([^;]*?\)\s*)?\w+\s*\(",
                                 body):
                        hits.setdefault(name, []).append(frel)
        if not hits:
            return ""
        parts = [f"`{n}` is instantiated in {', '.join(sorted(set(f)))}"
                 for n, f in hits.items()]
        return (f"You changed the PORT LIST of {', '.join('`'+n+'`' for n in hits)} "
                f"in {rel}. {'; '.join(parts)} — update those instances NOW, "
                f"in this turn, before running lint (a stale instance is "
                f"exactly what PINNOTFOUND errors mean).")

    def _checkpoint_files(self, job: str) -> set:
        """Files captured by a green checkpoint (empty if none/unreadable)."""
        import json
        import os
        mp = os.path.join(str(self.ctx.ws.dot), "checkpoints", job,
                          "manifest.json")
        try:
            return set(json.load(open(mp)).get("files", []))
        except (OSError, ValueError):
            return set()

    def _save_green_checkpoint(self, job: str, authored) -> None:
        """A passing check is a state worth being able to RETURN to: snapshot
        every authored file under .chipchamp/checkpoints/<job>/ so
        checkpoint.restore can undo a post-green edit tangle. Newest three
        checkpoints are kept."""
        import json
        import os
        import shutil
        if not job or not authored:
            return
        base = os.path.join(str(self.ctx.ws.dot), "checkpoints")
        d = os.path.join(base, job)
        files = []
        for rel in sorted(authored):
            src = os.path.join(self.ctx.ws.root, rel)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(d, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            files.append(rel)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as fh:
            json.dump({"job": job, "files": files}, fh)
        kids = sorted(os.listdir(base),
                      key=lambda n: os.path.getmtime(os.path.join(base, n)))
        for old_ck in kids[:-3]:
            shutil.rmtree(os.path.join(base, old_ck), ignore_errors=True)

    def _exec(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
        if not tool:
            # The distinction matters to whoever reads the error. A tool that
            # EXISTS but is outside this loop's set is least-privilege doing
            # its job (a subagent role, FR-MA-01) — telling that agent the
            # tool is "unknown" says the capability does not exist anywhere,
            # which is false and, rendered in a shared event stream, reads as
            # platform breakage to the human watching. Say which case it is.
            from ..tools import all_tools as _all
            if name in _all():
                return {"error": f"'{name}' exists but is not available in "
                                 f"this agent's tool set (least-privilege "
                                 f"role). Work with the tools you have, or "
                                 f"report the need back instead of retrying.",
                        "your_tools": sorted(self.tools)[:40]}
            if name in ("tool", "tools", "function", "functions",
                        "function_call", "tool_call"):
                # a model deep in a long context sometimes wraps a real call
                # in an envelope: tool({"functions": {"fs__read": {...}}}).
                # Teach the protocol instead of a bare unknown-tool error.
                return {"error": f"'{name}' is not a tool — there is no "
                                 f"wrapper. Call the tool directly by its "
                                 f"dotted name, e.g. fs.read({{\"path\": "
                                 f"...}}), one call per step.",
                        "your_tools": sorted(self.tools)[:40]}
            import difflib
            close = difflib.get_close_matches(name, list(self.tools), n=3,
                                              cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return {"error": f"unknown tool {name}.{hint}"}
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
        # the execution policy itself (coercion, budget, bad-args teaching,
        # crash shaping, cached flag, injection scan) lives in tools.executor
        # so the MCP server and the CLI run tools the same way
        result = invoke(self.ctx, tool, args, on_event=self.on_event, name=name)
        if is_job:
            self.on_event("job_done", {"name": name, "result": result})
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
    return stringify(result)


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
