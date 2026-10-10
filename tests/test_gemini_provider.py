"""Gemini selection and signature replay, with mocked HTTP/model responses."""
import json

import pytest

from chipchamp.agent.providers.registry import ModelRegistry
from chipchamp.agent.providers.openai_compat import (
    assemble_sse, parse_openai_response, to_openai_messages,
)
from chipchamp.agent.session import Session


@pytest.fixture
def clean_env(monkeypatch):
    # Environment choice must be deterministic, with no actual API key used.
    monkeypatch.setattr('os.environ', {})


def call(index=0):
    return {'index': index, 'id': f'call-gemini-{index}', 'type': 'function',
            'function': {'name': 'fs__read', 'arguments': '{"path":"top.sv"}'},
            'extra_content': {'google': {'thought_signature': f'opaque-signature-{index}'}}}


def replay(response):
    transcript = [{'role': 'user', 'content': 'Inspect the design'},
                  {'role': 'assistant', 'content': response.text, 'tool_calls': response.tool_calls},
                  {'role': 'tool', 'content': [{'id': c['id'], 'name': c['name'], 'output': 'module top; endmodule'}
                                             for c in response.tool_calls]}]
    return transcript, to_openai_messages('System', transcript)


def test_nonstream_signature_and_result_pairing():
    original = call()
    response = parse_openai_response({'choices': [{'message': {'content': '', 'tool_calls': [original]}}]})
    _, wire = replay(response)
    assert wire[2]['tool_calls'][0]['extra_content'] == original['extra_content']
    assert wire[2]['tool_calls'][0]['id'] == wire[3]['tool_call_id']
    assert json.loads(wire[2]['tool_calls'][0]['function']['arguments']) == {'path': 'top.sv'}


@pytest.mark.parametrize('late_signature', [False, True])
def test_streamed_parallel_calls_keep_their_own_signatures(late_signature):
    frames = []
    for index in (1, 0):
        original = call(index)
        first = {**original, 'function': {'name': 'fs__read', 'arguments': '{"path":'}}
        if late_signature:
            first.pop('extra_content')
        frames.append({'choices': [{'delta': {'tool_calls': [first]}}]})
        tail = {'index': index, 'function': {'arguments': '"top.sv"}'}}
        frames.append({'choices': [{'delta': {'tool_calls': [tail]}}]})
        if late_signature:
            frames.append({'choices': [{'delta': {'tool_calls': [{'index': index, 'extra_content': original['extra_content']}]}}]})
    frames.append({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 30}})
    stream = [('data: ' + json.dumps(f) + '\n').encode() for f in frames] + [b'data: [DONE]\n']
    response = parse_openai_response(assemble_sse(stream, lambda *_: None))
    _, wire = replay(response)
    for index in (0, 1):
        assert wire[2]['tool_calls'][index]['extra_content'] == call(index)['extra_content']
    assert (response.input_tokens, response.output_tokens) == (20, 30)


def test_session_resume_keeps_opaque_signature(tmp_path):
    response = parse_openai_response({'choices': [{'message': {'tool_calls': [call()]}}]})
    transcript, _ = replay(response)
    session = Session.new(str(tmp_path))
    session.messages = transcript
    session.save()
    restored = Session.load(session.id, str(tmp_path))
    wire = to_openai_messages('System', restored.messages)
    assert wire[2]['tool_calls'][0]['extra_content'] == call()['extra_content']


def test_plain_calls_do_not_gain_gemini_fields():
    ordinary = call()
    ordinary.pop('extra_content')
    response = parse_openai_response({'choices': [{'message': {'tool_calls': [ordinary]}}]})
    _, wire = replay(response)
    assert 'extra_content' not in wire[2]['tool_calls'][0]


def test_gemini_key_selects_google_and_omits_unset_reasoning(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-only-key')
    provider, model, reason = ModelRegistry({}, str(tmp_path)).resolve()
    assert reason == 'ok' and provider.name == 'gemini'
    assert model == 'gemini-3.1-pro-preview'
    captured = []
    def fake_post(path, payload):
        captured.append(provider._request(path, payload))
        return {'choices': [{'message': {'content': 'done'}}]}
    monkeypatch.setattr(provider, '_post', fake_post)
    provider.chat(model, 'System', [{'role': 'user', 'content': 'test'}], [])
    request = captured[0]
    assert request.full_url == 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    assert request.headers['Authorization'] == 'Bearer test-only-key'
    assert 'reasoning_effort' not in json.loads(request.data)


def test_existing_provider_precedence_preserved(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-only-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    reg = ModelRegistry({}, str(tmp_path))
    assert reg.current()['provider'] == 'openai'
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-key')
    assert reg.current()['provider'] == 'anthropic'
    explicit = ModelRegistry({'model': {'provider': 'ollama', 'model': 'local-model'}}, str(tmp_path))
    assert explicit.current()['provider'] == 'ollama'
