"""Provider-agnostic model layer: neutral-transcript translation to OpenAI and
Anthropic wire formats, tool-call parsing, and registry selection/persistence.
Pure functions + a mocked HTTP — no network."""
from __future__ import annotations

from chipchamp.agent.providers import ModelRegistry, ProviderConfig
from chipchamp.agent.providers.anthropic_provider import (to_anthropic_messages,
                                                         to_anthropic_tools)
from chipchamp.agent.providers.openai_compat import (parse_openai_response,
                                                    to_openai_messages,
                                                    to_openai_tools)

TRANSCRIPT = [
    {"role": "user", "content": "find the bug"},
    {"role": "assistant", "content": "checking", "tool_calls": [
        {"id": "c1", "name": "design__module", "input": {"module": "sync_fifo"}}]},
    {"role": "tool", "content": [
        {"id": "c1", "name": "design.module", "output": '{"ok":true}'}]},
]
TOOLS = [{"name": "design__module", "description": "card", "schema": {"type": "object"}}]


def test_openai_message_translation():
    import re
    msgs = to_openai_messages("SYS", TRANSCRIPT)
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["role"] == "user"
    asst = msgs[2]
    assert asst["role"] == "assistant"
    assert asst["tool_calls"][0]["function"]["name"] == "design__module"
    # ids are normalized to the strictest template's shape (9 alphanumerics,
    # Mistral) and the tool result stays keyed to its call
    wid = asst["tool_calls"][0]["id"]
    assert re.fullmatch(r"[a-zA-Z0-9]{9}", wid)
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == wid


def test_openai_adjacent_text_turns_coalesce():
    # strict templates demand user/assistant alternation; back-to-back text
    # turns (narration streaks past the nudge cap) must merge on the wire
    tr = [{"role": "user", "content": "go"},
          {"role": "assistant", "content": "first thought"},
          {"role": "assistant", "content": "second thought"}]
    msgs = to_openai_messages("SYS", tr)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert "first thought" in msgs[2]["content"]
    assert "second thought" in msgs[2]["content"]


def test_openai_tool_schema():
    t = to_openai_tools(TOOLS)[0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "design__module"
    assert t["function"]["parameters"] == {"type": "object"}


def test_openai_response_parse_toolcall():
    data = {"choices": [{"message": {"content": "",
             "tool_calls": [{"id": "x", "function": {
                 "name": "sim__run", "arguments": '{"test":"fifo_smoke","seed":3}'}}]},
             "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    r = parse_openai_response(data)
    assert r.tool_calls[0]["name"] == "sim__run"
    assert r.tool_calls[0]["input"] == {"test": "fifo_smoke", "seed": 3}
    assert r.input_tokens == 10 and r.output_tokens == 5


def test_anthropic_message_translation():
    msgs = to_anthropic_messages(TRANSCRIPT)
    asst = msgs[1]["content"]
    assert any(b["type"] == "tool_use" and b["name"] == "design__module" for b in asst)
    # tool results are a user turn with tool_result blocks
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0]["type"] == "tool_result"


def test_anthropic_tool_schema():
    t = to_anthropic_tools(TOOLS)[0]
    assert t["name"] == "design__module" and "input_schema" in t


def test_registry_presets_include_local_servers():
    reg = ModelRegistry()
    cfgs = reg.provider_configs()
    assert {"anthropic", "openai", "ollama", "lmstudio", "vllm"} <= set(cfgs)
    assert cfgs["ollama"].local and cfgs["ollama"].kind == "openai"


def test_registry_custom_provider_from_config():
    cfg = {"providers": {"corp": {"kind": "openai",
                                  "base_url": "https://llm.corp/v1",
                                  "api_key_env": "CORP_KEY",
                                  "default_model": "llama-70b"}}}
    reg = ModelRegistry(cfg)
    corp = reg.provider_configs()["corp"]
    assert corp.base_url == "https://llm.corp/v1" and corp.default_model == "llama-70b"


def test_registry_selection_persist_and_resolve(tmp_path):
    reg = ModelRegistry({}, str(tmp_path))
    reg.save_selection("ollama", "llama3.1:8b")
    assert reg.saved_selection()["model"] == "llama3.1:8b"
    prov, model, reason = reg.resolve()
    assert reason == "ok" and prov.name == "ollama" and model == "llama3.1:8b"


def test_registry_config_model_selection():
    reg = ModelRegistry({"model": {"provider": "openai", "model": "gpt-4o-mini"}}, ".")
    # no selection file, no env -> falls to config
    cur = reg.current()
    assert cur and cur["provider"] == "openai" and cur["model"] == "gpt-4o-mini"


def test_parse_openai_reasoning_content_and_think_tags():
    from chipchamp.agent.providers.openai_compat import parse_openai_response
    # LM Studio reasoning split: content empty, reasoning_content populated
    r = parse_openai_response({"choices": [{"message": {
        "content": "", "reasoning_content": "chain of thought"},
        "finish_reason": "length"}], "usage": {}})
    assert r.text == "" and r.reasoning == "chain of thought"
    assert r.stop_reason == "length"
    # inline <think> template: reasoning is stripped out of the text
    r2 = parse_openai_response({"choices": [{"message": {
        "content": "<think>hmm</think>\nThe answer is 4."},
        "finish_reason": "stop"}], "usage": {}})
    assert r2.text == "The answer is 4." and "hmm" in r2.reasoning
