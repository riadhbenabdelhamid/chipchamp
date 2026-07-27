"""Text-embedded tool-call recovery: the formats local templates leak."""
from __future__ import annotations

from chipchamp.agent.providers.base import ModelResponse
from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.toolcalls import recover_tool_calls, strip_tool_call_text

KNOWN = {"fs__list", "fs.list", "design__cone", "design.cone",
         "sim__run", "sim.run", "report__done", "report.done"}


def test_recover_json_style():
    txt = ('I will list the files.\n<tool_call>\n'
           '{"name": "fs__list", "arguments": {"dir": "rtl"}}\n</tool_call>')
    calls = recover_tool_calls(txt, "", KNOWN)
    assert len(calls) == 1
    assert calls[0]["name"] == "fs__list" and calls[0]["input"] == {"dir": "rtl"}
    assert "tool_call" not in strip_tool_call_text(txt)


def test_recover_function_tag_style_truncated():
    # the exact shape observed live from qwen3.5-9b via LM Studio — second
    # call truncated mid-generation, no closing tags
    txt = """<tool_call>
<function=fs_list>
<parameter=dir>
examples/soc/rtl
</parameter>
</function>
</tool_call>
<tool_call>
<function=design_cone>
<parameter=module>
counter
</parameter>
<parameter=signal>
q
</parameter>
<parameter=direction>
fanin"""
    known = KNOWN | {"fs_list", "design_cone"}
    calls = recover_tool_calls(txt, "", known)
    assert [c["name"] for c in calls] == ["fs_list", "design_cone"]
    assert calls[0]["input"] == {"dir": "examples/soc/rtl"}
    assert calls[1]["input"]["module"] == "counter"
    assert calls[1]["input"]["direction"] == "fanin"  # truncated tail recovered


def test_recover_from_reasoning_channel_and_fenced_json():
    calls = recover_tool_calls(
        "", 'thinking… ```json\n{"tool": "sim.run", "parameters": '
            '{"test": "fifo_smoke"}}\n```', KNOWN)
    assert calls and calls[0]["name"] == "sim.run"
    assert calls[0]["input"] == {"test": "fifo_smoke"}


def test_prose_mentions_never_trigger():
    txt = "You could call fs.list or sim.run here, but I already know the answer."
    assert recover_tool_calls(txt, "", KNOWN) == []
    assert recover_tool_calls("", "maybe I should use design.cone…", KNOWN) == []


def test_unknown_names_rejected():
    txt = '<tool_call>{"name": "rm__rf", "arguments": {}}</tool_call>'
    assert recover_tool_calls(txt, "", KNOWN) == []


def test_loop_executes_recovered_calls(ctx):
    class FakeGateway:
        available = True
        ref = "fake:test"
        model = "fake"

        def __init__(self, script):
            self.script = list(script)

        def complete(self, system, transcript, tools, audit_items=None):
            return self.script.pop(0)

    script = [
        ModelResponse(text='<tool_call>\n{"name": "design__module", '
                           '"arguments": {"module": "sync_fifo"}}\n</tool_call>',
                      tool_calls=[], stop_reason="stop"),
        ModelResponse(text="done.", tool_calls=[], stop_reason="stop"),
    ]
    events = []
    loop = AgentLoop(ctx, FakeGateway(script),
                     on_event=lambda k, d: events.append((k, d)))
    out = loop.run("describe sync_fifo")
    assert out["recovered_calls"] == 1
    assert out["text"] == "done."
    # the recovered call actually executed against the real tool
    assert any(k == "tool_result" and d["result"].get("card", {}).get("name")
               == "sync_fifo" for k, d in events)


def test_recover_mistral_tekken_style():
    # devstral leak observed on a router handoff: [TOOL_CALLS]name[ARGS]{json}
    text = ('[TOOL_CALLS]fs__list[ARGS]{"dir": "work/rtl"}')
    calls = recover_tool_calls(text, "", KNOWN)
    assert [(c["name"], c["input"]) for c in calls] == \
        [("fs__list", {"dir": "work/rtl"})]
    assert strip_tool_call_text(text) == ""


def test_recover_mistral_array_style():
    text = ('prefix prose [TOOL_CALLS][{"name": "sim.run", "arguments": '
            '{"test": "smoke"}}] suffix')
    calls = recover_tool_calls(text, "", KNOWN)
    assert calls and calls[0]["name"] == "sim.run"
    assert calls[0]["input"] == {"test": "smoke"}
    stripped = strip_tool_call_text(text)
    assert "TOOL_CALLS" not in stripped and "prefix prose" in stripped


def test_mistral_prose_mention_never_triggers():
    # bare [TOOL_CALLS] with no JSON payload must not execute anything
    calls = recover_tool_calls("the template emits [TOOL_CALLS] then args",
                               "", {"fs.write"})
    assert calls == []
