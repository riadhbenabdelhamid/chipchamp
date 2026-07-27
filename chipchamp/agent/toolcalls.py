"""Text-embedded tool-call recovery (FR-MDL robustness for local models).

Local servers parse a model's native tool-call syntax into structured
``tool_calls`` — but reasoning models frequently emit a *variant* the server's
template parser misses (wrong tag, calls inside the think channel, truncated
closers). The call then arrives as plain text and a naive loop concludes "the
model didn't call a tool". This module recovers those calls.

Recovery is conservative: a candidate only counts when its name resolves to a
known tool (dotted or dunder form), so prose that merely *mentions* a tool
never triggers execution. Formats covered (seen in the wild across Qwen,
Hermes/LLaMA and DeepSeek templates):

  A. ``<tool_call>{"name": "fs.list", "arguments": {…}}</tool_call>``
  B. ``<tool_call><function=fs_list><parameter=dir>rtl</parameter>…`` —
     including truncated tails (generation cut mid-parameter)
  C. a fenced ```json block whose object carries name + arguments/parameters
  D. Mistral-template leaks: ``[TOOL_CALLS]fs_write[ARGS]{…}`` (tekken) and
     ``[TOOL_CALLS][{"name": …, "arguments": …}, …]`` — seen from devstral
     when a router handoff replays another model's transcript
"""
from __future__ import annotations

import json
import re


def _norm(name: str, known: set[str]) -> str | None:
    """Resolve a candidate tool name against the catalog (both spellings)."""
    for cand in (name, name.replace("__", "."), name.replace(".", "__")):
        if cand in known:
            return cand
    return None


def _coerce(val: str):
    v = val.strip()
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v


def _from_json_obj(obj: dict, known: set[str]) -> dict | None:
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if isinstance(name, dict):  # {"function": {"name": …, "arguments": …}}
        obj = name
        name = obj.get("name")
    if not isinstance(name, str):
        return None
    resolved = _norm(name.strip(), known)
    if not resolved:
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("input", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    return {"name": resolved, "input": args}


_TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)",
                              re.DOTALL)
_FUNC_TAG = re.compile(r"<function=([\w.\-]+)>")
_PARAM_TAG = re.compile(
    r"<parameter=([\w.\-]+)>\s*(.*?)\s*(?:</parameter>|(?=<parameter=)|$)",
    re.DOTALL)
_FENCED_JSON = re.compile(r"```(?:json|tool)?\s*(\{.*?\})\s*```", re.DOTALL)
_MISTRAL_HDR = re.compile(r"\[TOOL_CALLS\]")
_MISTRAL_NAMED = re.compile(r"\[TOOL_CALLS\]\s*([\w.\-]+)\s*\[ARGS\]\s*")


def _mistral_candidates(payload: str) -> list[tuple]:
    """(name_or_None, obj, (start, end)) for Mistral-template leaks. The named
    (tekken) form carries the args object directly; the array form carries
    {"name": …, "arguments": …} items. raw_decode keeps trailing prose intact."""
    dec = json.JSONDecoder()
    out: list[tuple] = []
    for m in _MISTRAL_NAMED.finditer(payload):
        try:
            obj, end = dec.raw_decode(payload, m.end())
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append((m.group(1), obj, (m.start(), end)))
    if out:
        return out
    for m in _MISTRAL_HDR.finditer(payload):
        i = m.end()
        while i < len(payload) and payload[i] in " \n\t":
            i += 1
        if i >= len(payload) or payload[i] not in "[{":
            continue
        try:
            obj, end = dec.raw_decode(payload, i)
        except ValueError:
            continue
        for item in (obj if isinstance(obj, list) else [obj]):
            if isinstance(item, dict):
                out.append((None, item, (m.start(), end)))
    return out


def recover_tool_calls(text: str, reasoning: str, known_names) -> list[dict]:
    """Extract tool calls a server-side parser missed. Returns
    ``[{id, name, input}]`` (ids synthesized); empty when nothing safe."""
    known = set(known_names)
    calls: list[dict] = []

    def scan(payload: str) -> None:
        for block in _TOOL_CALL_BLOCK.findall(payload or ""):
            block = block.strip()
            got = None
            if block.startswith("{"):
                try:
                    got = _from_json_obj(json.loads(block), known)
                except (json.JSONDecodeError, ValueError):
                    got = None
            if got is None:
                m = _FUNC_TAG.search(block)
                if m:
                    name = _norm(m.group(1), known)
                    if name:
                        args = {k: _coerce(v)
                                for k, v in _PARAM_TAG.findall(block)}
                        got = {"name": name, "input": args}
            if got:
                calls.append(got)
        if not calls:
            for name, obj, _span in _mistral_candidates(payload or ""):
                if name:
                    resolved = _norm(name, known)
                    if resolved:
                        calls.append({"name": resolved, "input": obj})
                else:
                    got = _from_json_obj(obj, known)
                    if got:
                        calls.append(got)
        if not calls:
            for block in _FENCED_JSON.findall(payload or ""):
                try:
                    got = _from_json_obj(json.loads(block), known)
                except (json.JSONDecodeError, ValueError):
                    got = None
                if got:
                    calls.append(got)

    scan(text)
    if not calls:
        scan(reasoning)
    for i, c in enumerate(calls):
        c["id"] = f"recovered-{i}"
    return calls


def strip_tool_call_text(text: str) -> str:
    """Remove recovered call markup from the visible text."""
    out = _TOOL_CALL_BLOCK.sub("", text or "")
    for _name, _obj, span in sorted(_mistral_candidates(out),
                                    key=lambda c: c[2], reverse=True):
        out = out[:span[0]] + out[span[1]:]
    return out.strip()
