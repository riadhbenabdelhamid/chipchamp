"""Agent loop mechanics with a scripted gateway (no network):
tool dispatch → execute → feed result back → final text, plus audit logging."""
from __future__ import annotations

import json

from chipchamp.agent.providers.base import ModelResponse
from chipchamp.agent.loop import AgentLoop


class FakeGateway:
    """Returns a scripted sequence of responses; records audit items."""
    available = True
    model = "fake"
    ref = "fake:test"

    def __init__(self, script):
        self.script = list(script)
        self.audit_calls = []

    def complete(self, system, transcript, tools, audit_items=None):
        self.audit_calls.append(audit_items or [])
        return self.script.pop(0)


def test_loop_dispatches_tools_and_feeds_results(ctx):
    # 1) model asks for design.module; 2) model gives a final answer
    tool_call = {"id": "tu1", "name": "design__module", "input": {"module": "sync_fifo"}}
    script = [
        ModelResponse(text="", tool_calls=[tool_call], stop_reason="tool_use"),
        ModelResponse(text="sync_fifo has 9 ports.", tool_calls=[],
                      stop_reason="end_turn"),
    ]
    gw = FakeGateway(script)
    events = []
    loop = AgentLoop(ctx, gw, on_event=lambda k, d: events.append((k, d)))
    out = loop.run("describe sync_fifo")
    assert out["steps"] == 2
    assert "9 ports" in out["text"]
    # the design.module tool actually ran against the real DB
    tool_results = [e for e in events if e[0] == "tool_result"]
    assert tool_results and tool_results[0][1]["result"]["card"]["name"] == "sync_fifo"
    # audit log captured a context item on the second call (the tool result)
    assert any(gw.audit_calls)


def test_loop_maps_dotted_tool_names(ctx):
    # ensure the __ <-> . mapping round-trips for the API
    loop = AgentLoop(ctx, FakeGateway([]))
    assert loop._name_map["design__hierarchy"] == "design.hierarchy"
    assert any(t["name"] == "report__done" for t in loop._api_tools)


def test_unknown_tool_is_handled(ctx):
    script = [ModelResponse(text="", tool_calls=[
        {"id": "x", "name": "nope__tool", "input": {}}]),
        ModelResponse(text="done", tool_calls=[])]
    loop = AgentLoop(ctx, FakeGateway(script))
    out = loop.run("do something")
    assert out["steps"] == 2  # loop recovered from the unknown tool


def test_empty_reasoning_turn_nudges_instead_of_ending(ctx):
    """A reasoning model that burns max_tokens (finish_reason=length, all
    output in reasoning_content) must be nudged to act, not silently ended."""
    script = [
        ModelResponse(text="", tool_calls=[], stop_reason="length",
                      reasoning="thinking forever…"),
        ModelResponse(text="final answer.", tool_calls=[], stop_reason="end_turn"),
    ]
    loop = AgentLoop(ctx, FakeGateway(script))
    out = loop.run("answer the question")
    assert out["text"] == "final answer."
    assert out["empty_turns"] == 1 and out["length_stops"] == 1


def test_repeated_empty_turns_fail_loudly(ctx):
    script = [ModelResponse(text="", tool_calls=[], stop_reason="length")
              for _ in range(5)]
    loop = AgentLoop(ctx, FakeGateway(script))
    out = loop.run("answer")
    assert "token limit" in out["text"]  # explicit failure, not silence
    assert out["empty_turns"] == 3


def test_transient_provider_glitch_is_retried(ctx):
    """One bad generation (e.g. LM Studio 'peg-native format' 400/500) must
    not kill the task; auth errors must not be retried."""
    calls = {"n": 0}

    class GlitchyGateway(FakeGateway):
        def complete(self, system, transcript, tools, audit_items=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    "HTTP 400 from lmstudio: The model produced output that "
                    "does not match the expected peg-native format")
            return ModelResponse(text="recovered fine.", tool_calls=[])

    loop = AgentLoop(ctx, GlitchyGateway([]))
    out = loop.run("do a thing")
    assert out["text"] == "recovered fine." and calls["n"] == 2
    assert out["call_retries"] == 1

    class AuthGateway(FakeGateway):
        def complete(self, *a, **k):
            raise RuntimeError("HTTP 401 from lmstudio: unauthorized")

    out2 = AgentLoop(ctx, AuthGateway([])).run("x")
    assert out2.get("error") and "401" in out2["text"]


def test_job_tools_emit_start_and_done_events(ctx):
    """Metered/licensed tools (real EDA jobs) announce start+finish so the UI
    can show the running subtask instead of dead air (job visibility)."""
    from chipchamp.tools.base import Tool

    def fake_sim(ctx, **kw):
        return {"job": "J-9001", "status": "passed", "summary": "sim ok"}

    tools = {"sim.run": Tool(name="sim.run", description="run", cost="metered",
                             permission="submit", schema={"type": "object",
                             "properties": {}}, handler=fake_sim)}
    script = [
        ModelResponse(text="", tool_calls=[
            {"id": "1", "name": "sim__run", "input": {}}]),
        ModelResponse(text="done", tool_calls=[]),   # nudged (submit w/o report.done)
        ModelResponse(text="done", tool_calls=[]),   # 2nd text → accepted as final
    ]
    events = []
    loop = AgentLoop(ctx, FakeGateway(script), tools=tools,
                     on_event=lambda k, d: events.append((k, d)))
    loop.run("simulate it")
    kinds = [k for k, _ in events]
    assert "job_start" in kinds and "job_done" in kinds
    done = next(d for k, d in events if k == "job_done")
    assert done["result"]["job"] == "J-9001"
    # a free/read tool must NOT emit job events
    assert kinds.index("job_start") < kinds.index("job_done")


def test_free_tools_emit_no_job_events(ctx):
    tool_call = {"id": "t", "name": "design__module", "input": {"module": "sync_fifo"}}
    script = [ModelResponse(text="", tool_calls=[tool_call]),
              ModelResponse(text="ok", tool_calls=[])]
    events = []
    loop = AgentLoop(ctx, FakeGateway(script),
                     on_event=lambda k, d: events.append(k))
    loop.run("describe")
    assert "job_start" not in events  # design.module is free/read


def test_truncated_turn_with_text_is_continued_not_ended(ctx):
    """A turn cut off at the token limit (finish_reason=length) that has a
    scrap of text ('let me rewrite…') must NOT be treated as a final answer —
    the loop nudges it to continue and it can then emit its tool call."""
    tool_call = {"id": "1", "name": "design__module", "input": {"module": "sync_fifo"}}
    script = [
        # truncated mid-thought: some text, no tool call, stop_reason=length
        ModelResponse(text="The initial code has issues. Let me rewrite it cleanly.",
                      tool_calls=[], stop_reason="length"),
        # after the nudge, it emits the tool call it was cut off from
        ModelResponse(text="", tool_calls=[tool_call], stop_reason="tool_use"),
        ModelResponse(text="done.", tool_calls=[], stop_reason="end_turn"),
    ]
    events = []
    loop = AgentLoop(ctx, FakeGateway(script),
                     on_event=lambda k, d: events.append((k, d)))
    out = loop.run("create a module")
    assert out["text"] == "done."          # did NOT end on the truncated intro
    assert out["empty_turns"] == 1          # counted the stall
    assert out["length_stops"] == 1
    # the recovered tool actually ran
    assert any(k == "tool_result" and d["result"].get("card", {}).get("name")
               == "sync_fifo" for k, d in events)


def test_complete_text_turn_still_ends(ctx):
    """A normal end_turn with text is still a final answer (no spurious nudge)."""
    script = [ModelResponse(text="here is the answer.", tool_calls=[],
                            stop_reason="end_turn")]
    loop = AgentLoop(ctx, FakeGateway(script))
    out = loop.run("a question")
    assert out["text"] == "here is the answer." and out["empty_turns"] == 0


def test_readonly_query_then_text_ends_without_nudge(ctx):
    """A read-only lookup ("describe X" → design.module → prose answer) is a
    genuine final answer — the narration nudge must NOT fire (no mutating work)."""
    script = [
        ModelResponse(text="", tool_calls=[
            {"id": "t", "name": "design__module", "input": {"module": "sync_fifo"}}]),
        ModelResponse(text="sync_fifo has 9 ports.", tool_calls=[], stop_reason="end_turn"),
    ]
    out = AgentLoop(ctx, FakeGateway(script)).run("describe sync_fifo")
    assert out["steps"] == 2 and "9 ports" in out["text"]


def _mutating_tools():
    from chipchamp.tools.base import Tool
    return {
        "fs.write": Tool(name="fs.write", description="w", cost="cheap",
                         permission="write",
                         schema={"type": "object", "properties": {}},
                         handler=lambda ctx, **k: {"written": ["work/rtl/x.sv"]}),
        "report.done": Tool(name="report.done", description="done", cost="cheap",
                            permission="submit",
                            schema={"type": "object", "properties": {}},
                            handler=lambda ctx, **k: {"accepted": True,
                                                      "message": "verified"}),
    }


def test_narration_after_mutation_is_nudged_not_ended(ctx):
    """After mutating work (fs.write), a text turn that narrates instead of
    calling report.done must be nudged, not accepted as the final answer."""
    tools = _mutating_tools()
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "fs__write", "input": {}}]),
        ModelResponse(text="I wrote the module; next I will lint it.", tool_calls=[]),
        ModelResponse(text="", tool_calls=[{"id": "2", "name": "report__done", "input": {}}]),
        ModelResponse(text="all gates satisfied.", tool_calls=[]),
    ]
    out = AgentLoop(ctx, FakeGateway(script), tools=tools).run("build it")
    assert out["steps"] == 4                         # the narration did NOT end it
    assert out["text"] == "all gates satisfied."     # ended after report.done accepted


def test_narration_nudge_is_bounded(ctx):
    """Two text-only turns in a row (model won't act) are accepted as final —
    the nudge never loops forever."""
    tools = _mutating_tools()
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "fs__write", "input": {}}]),
        ModelResponse(text="narration one", tool_calls=[]),   # nudged
        ModelResponse(text="narration two", tool_calls=[]),   # 2nd in a row → final
    ]
    out = AgentLoop(ctx, FakeGateway(script), tools=tools).run("build it")
    assert out["steps"] == 3 and out["text"] == "narration two"


def test_text_after_accepted_report_done_ends_cleanly(ctx):
    """Once report.done is accepted, a following summary text turn is a
    legitimate final answer — no nudge."""
    tools = _mutating_tools()
    script = [
        ModelResponse(text="", tool_calls=[{"id": "1", "name": "report__done", "input": {}}]),
        ModelResponse(text="summary of the verified work.", tool_calls=[]),
    ]
    out = AgentLoop(ctx, FakeGateway(script), tools=tools).run("finish")
    assert out["steps"] == 2 and out["text"] == "summary of the verified work."


# ---- model router: auto-switch on failure signatures --------------------------

from chipchamp.agent.router import ModelRouter, RouterConfig       # noqa: E402
from chipchamp.agent.providers.registry import ModelRegistry        # noqa: E402
from chipchamp.agent.gateway import ModelTimeout                    # noqa: E402


class RefGateway(FakeGateway):
    """A FakeGateway with a distinct ref (so the router can tell models apart)."""
    def __init__(self, script, ref):
        super().__init__(script)
        self.ref = ref
        self.available = True
        self.max_tokens = 16384
        self.timeout = None
        self.audit = None


class TimeoutGateway(RefGateway):
    def complete(self, *a, **k):
        raise ModelTimeout("model call exceeded 1s")


def _router_with(gws, pool):
    """A real ModelRouter (real ranking/next_fallback/is_refusal) but with
    build_gateway stubbed to hand back the scripted fake gateways."""
    r = ModelRouter(ModelRegistry({}, "."), RouterConfig(enabled=True, pool=list(pool)), {})
    r.build_gateway = lambda ref, like=None: gws.get(ref)
    return r


def test_router_switches_on_refusal(ctx):
    g1 = RefGateway([ModelResponse(text="I'm sorry, but I can't continue with this task.",
                                   tool_calls=[])], "m1")
    g2 = RefGateway([ModelResponse(text="Here is the completed answer.", tool_calls=[])], "m2")
    out = AgentLoop(ctx, g1, router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"])).run("build it")
    assert [s["reason"] for s in out["switches"]] == ["refusal"]
    assert out["models_used"] == ["m1", "m2"]
    assert out["text"] == "Here is the completed answer."


def test_router_switches_on_timeout(ctx):
    g1 = TimeoutGateway([], "m1")
    g2 = RefGateway([ModelResponse(text="done on the fallback.", tool_calls=[])], "m2")
    out = AgentLoop(ctx, g1, router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"])).run("x")
    assert [s["reason"] for s in out["switches"]] == ["timeout"]
    assert out["text"] == "done on the fallback."


def test_router_switches_on_stall_to_debugger(ctx):
    from chipchamp.tools.base import Tool
    tools = {"lint.run": Tool(name="lint.run", description="l", cost="cheap",
             permission="submit", schema={"type": "object", "properties": {}},
             handler=lambda ctx, **k: {"status": "failed", "errors": 5})}
    lint = {"id": "1", "name": "lint__run", "input": {}}
    g1 = RefGateway([ModelResponse(text="", tool_calls=[lint]) for _ in range(3)], "m1")
    # after the stall switch the fallback still faces the narration nudge (a
    # mutating job ran, no report.done) — so it takes two text turns to end.
    g2 = RefGateway([ModelResponse(text="let me look", tool_calls=[]),
                     ModelResponse(text="fixed on the debugger.", tool_calls=[])], "m2")
    out = AgentLoop(ctx, g1, tools=tools,
                    router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"])).run("debug")
    assert any(s["reason"] == "stall" and s["to"] == "m2" for s in out["switches"])
    assert out["text"] == "fixed on the debugger."


def test_router_disabled_is_no_switch(ctx):
    g1 = RefGateway([ModelResponse(text="I can't continue.", tool_calls=[])], "m1")
    g2 = RefGateway([ModelResponse(text="unused", tool_calls=[])], "m2")
    router = _router_with({"m1": g1, "m2": g2}, ["m1", "m2"])
    router.cfg.enabled = False
    out = AgentLoop(ctx, g1, router=router).run("x")
    assert out["switches"] == [] and out["text"] == "I can't continue."


def test_stall_handoff_stays_template_legal(ctx):
    # A stall fires right after a TOOL result. Strict chat templates (Mistral)
    # reject a user turn directly after tool results, so the handoff must ride
    # in the system prompt — and the transcript must stay alternation-legal.
    from chipchamp.tools.base import Tool
    tools = {"lint.run": Tool(name="lint.run", description="l", cost="cheap",
             permission="submit", schema={"type": "object", "properties": {}},
             handler=lambda ctx, **k: {"status": "failed", "errors": 5})}
    lint = {"id": "1", "name": "lint__run", "input": {}}
    g1 = RefGateway([ModelResponse(text="", tool_calls=[lint]) for _ in range(3)], "m1")

    class SysCapture(RefGateway):
        def __init__(self, script, ref):
            super().__init__(script, ref)
            self.systems = []

        def complete(self, system, transcript, tools, audit_items=None):
            self.systems.append(system)
            self.transcripts = [dict(t) for t in transcript]
            return super().complete(system, transcript, tools, audit_items)

    g2 = SysCapture([ModelResponse(text="over.", tool_calls=[]),
                     ModelResponse(text="done.", tool_calls=[])], "m2")
    loop = AgentLoop(ctx, g1, tools=tools,
                     router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"]))
    out = loop.run("debug")
    assert any(s["reason"] == "stall" for s in out["switches"])
    # the fallback's first call: handoff present in SYSTEM, not the transcript
    assert "[router]" in g2.systems[0]
    tr = g2.transcripts
    for prev, cur in zip(tr, tr[1:]):
        assert not (prev["role"] == "tool" and cur["role"] == "user"), \
            "user turn directly after tool results breaks strict templates"


def test_plan_then_narrate_collapse_triggers_giveup_switch(ctx):
    # gpt-oss's signature collapse: declare a plan, then narrate in prose and
    # stop without a single mutating call. An unfinished plan counts as
    # engagement — the router must get the giveup, not read it as an answer.
    from chipchamp.tools import all_tools
    tools = {"plan.update": all_tools()["plan.update"]}
    plan = {"id": "1", "name": "plan__update",
            "input": {"steps": [{"step": "write rtl", "status": "pending"}]}}
    g1 = RefGateway([ModelResponse(text="", tool_calls=[plan]),
                     ModelResponse(text="Here is my plan in prose.", tool_calls=[]),
                     ModelResponse(text="Next I will fetch components.", tool_calls=[])],
                    "m1")
    g2 = RefGateway([ModelResponse(text="taking over.", tool_calls=[]),
                     ModelResponse(text="done.", tool_calls=[])], "m2")
    out = AgentLoop(ctx, g1, tools=tools,
                    router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"])).run("build it")
    assert any(s["reason"] == "giveup" and s["to"] == "m2" for s in out["switches"])


def test_plain_qa_answer_still_ends_without_switch(ctx):
    # no plan, no mutating call — a text answer is genuinely final
    g1 = RefGateway([ModelResponse(text="The design has 3 modules.", tool_calls=[])], "m1")
    g2 = RefGateway([ModelResponse(text="should not run", tool_calls=[])], "m2")
    out = AgentLoop(ctx, g1, tools={},
                    router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"])).run("how many modules?")
    assert out["switches"] == []
    assert out["text"] == "The design has 3 modules."


def test_router_switches_on_hard_model_failure(ctx):
    # a model whose server died (RuntimeError after retries) must hand off,
    # not end the task — qwen's llama-server OOM-died mid-run
    class DeadGateway(RefGateway):
        def complete(self, *a, **k):
            raise RuntimeError("HTTP 400 from lmstudio: failed to load model")

    g1 = DeadGateway([], "m1")
    g2 = RefGateway([ModelResponse(text="carried it home.", tool_calls=[])], "m2")
    out = AgentLoop(ctx, g1, tools={},
                    router=_router_with({"m1": g1, "m2": g2}, ["m1", "m2"])).run("build")
    assert any(s["reason"] == "error" and s["to"] == "m2" for s in out["switches"])
    assert out["text"] == "carried it home."


def test_hard_failure_without_router_still_fails_cleanly(ctx):
    class DeadGateway(RefGateway):
        def complete(self, *a, **k):
            raise RuntimeError("connection refused")

    out = AgentLoop(ctx, DeadGateway([], "m1"), tools={}).run("build")
    assert out.get("error") and "model call failed" in out["text"]


def _sim_tool(summaries):
    """A fake sim.run whose per-call result comes from `summaries`:
    a status string ('passed') or a failure summary text."""
    from chipchamp.tools.base import Tool
    it = iter(summaries)

    def handler(ctx, **k):
        s = next(it)
        if s == "passed":
            return {"status": "passed", "sim_status": "pass", "summary": "ok"}
        return {"status": "failed", "sim_status": "fail", "summary": s}
    return {"sim.run": Tool(name="sim.run", description="s", cost="metered",
            permission="submit", schema={"type": "object", "properties": {}},
            handler=handler)}


def _sim_call():
    return {"id": "1", "name": "sim__run",
            "input": {"test": "stream_merge_system_test"}}


def test_bad_oracle_nudge_after_identical_sim_failures(ctx):
    # The relay ground ~60 sim iterations against an unsatisfiable
    # model-authored oracle whose complaint never changed ("Cycle N: m_valid
    # mismatch") no matter what the RTL did. Three identical-signature
    # failures must inject the oracle-suspect nudge — and digit
    # normalization must treat a shifted cycle stamp as the SAME signature.
    tools = _sim_tool(["Cycle 11: m_valid mismatch",
                       "Cycle 13: m_valid mismatch",
                       "Cycle 27: m_valid mismatch"])
    g = RefGateway([ModelResponse(text="", tool_calls=[_sim_call()])
                    for _ in range(3)]
                   + [ModelResponse(text="hm.", tool_calls=[]),
                      ModelResponse(text="over.", tool_calls=[])], "m1")
    events = []
    transcripts = []
    orig = g.complete

    def complete(system, transcript, tools_, audit_items=None):
        transcripts.append([dict(t) for t in transcript])
        return orig(system, transcript, tools_, audit_items)
    g.complete = complete
    out = AgentLoop(ctx, g, tools=tools,
                    on_event=lambda k, d: events.append((k, d))).run("debug")
    suspects = [d for k, d in events if k == "oracle_suspect"]
    assert len(suspects) == 1
    assert suspects[0]["test"] == "stream_merge_system_test"
    assert suspects[0]["count"] == 3
    assert "Cycle N: m_valid mismatch" in suspects[0]["signature"]
    # the nudge reached the transcript the model actually sees
    assert any(t.get("role") == "user" and "ORACLE" in str(t.get("content"))
               for t in transcripts[-1])


def test_oracle_nudge_fires_once_per_signature(ctx):
    tools = _sim_tool([f"Cycle {n}: m_valid mismatch"
                       for n in (3, 5, 7, 9, 11)])
    g = RefGateway([ModelResponse(text="", tool_calls=[_sim_call()])
                    for _ in range(5)]
                   + [ModelResponse(text="hm.", tool_calls=[]),
                      ModelResponse(text="over.", tool_calls=[])], "m1")
    events = []
    AgentLoop(ctx, g, tools=tools,
              on_event=lambda k, d: events.append((k, d))).run("debug")
    assert sum(1 for k, _ in events if k == "oracle_suspect") == 1


def test_changing_failure_signatures_do_not_nudge(ctx):
    # a debug session making real progress fails DIFFERENTLY each time —
    # that must never be branded a bad oracle
    tools = _sim_tool(["m_valid mismatch", "data underflow", "chan_id illegal"])
    g = RefGateway([ModelResponse(text="", tool_calls=[_sim_call()])
                    for _ in range(3)]
                   + [ModelResponse(text="hm.", tool_calls=[]),
                      ModelResponse(text="over.", tool_calls=[])], "m1")
    events = []
    AgentLoop(ctx, g, tools=tools,
              on_event=lambda k, d: events.append((k, d))).run("debug")
    assert not any(k == "oracle_suspect" for k, _ in events)


def test_sim_pass_resets_oracle_signature_counter(ctx):
    tools = _sim_tool(["boom", "boom", "passed", "boom", "boom"])
    g = RefGateway([ModelResponse(text="", tool_calls=[_sim_call()])
                    for _ in range(5)]
                   + [ModelResponse(text="hm.", tool_calls=[]),
                      ModelResponse(text="over.", tool_calls=[])], "m1")
    events = []
    AgentLoop(ctx, g, tools=tools,
              on_event=lambda k, d: events.append((k, d))).run("debug")
    assert not any(k == "oracle_suspect" for k, _ in events)


def test_system_prompt_carries_oracle_laws(ctx):
    from chipchamp.agent.prompts import build_system_prompt
    sp = build_system_prompt(ctx.ws, "", "policy")
    assert "STREAM EQUALITY" in sp
    assert "valid must NEVER depend on ready" in sp


def test_crashing_event_callback_never_kills_the_run(ctx):
    # a UI observer bug (e.g. slicing a dict-valued summary) must not
    # propagate into the loop — one killed a 714s autonomous run
    def bad_observer(kind, data):
        raise KeyError(slice(None, 70, None))

    g = RefGateway([ModelResponse(text="answer.", tool_calls=[])], "m1")
    out = AgentLoop(ctx, g, tools={}, on_event=bad_observer).run("q")
    assert out["text"] == "answer."
