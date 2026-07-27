"""Web research tools (SPEC §9): search + fetch parsing, safety, and the
delegated web-researcher sub-agent — all offline (urllib is monkeypatched)."""
from __future__ import annotations

import pytest

from chipchamp.tools import web_tools as w


# ---- html / text ---------------------------------------------------------------


def test_html_to_text_strips_head_scripts_and_tags():
    page = ("<html><head><title>My  Page</title></head><body>"
            "<script>bad()</script><style>x{}</style>"
            "<h1>Hi</h1><p>Para one.</p><p>Two &amp; three</p></body></html>")
    assert w._title(page) == "My Page"
    text = w._html_to_text(page)
    assert "bad()" not in text and "x{}" not in text
    assert text == "Hi\nPara one.\nTwo & three"     # title not duplicated in body


# ---- search parsing ------------------------------------------------------------


def _patch_http(monkeypatch, body: bytes, ctype="text/html; charset=utf-8"):
    monkeypatch.setattr(w, "_http",
                        lambda url, timeout, **k: (200, ctype, body))


def test_ddg_search_parses_and_unwraps_redirects(monkeypatch):
    ddg = (
        '<a rel="nofollow" class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">'
        'First <b>Result</b></a>'
        '<a class="result__snippet" href="x">A snippet &amp; more</a>'
        '<a rel="nofollow" class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb">Second</a>'
        '<a class="result__snippet" href="y">Snip two</a>')
    _patch_http(monkeypatch, ddg.encode())
    res = w._ddg_search("q", 5, 5.0)
    assert res == [
        {"title": "First Result", "url": "https://example.com/a",
         "snippet": "A snippet & more"},
        {"title": "Second", "url": "https://example.org/b", "snippet": "Snip two"}]


def test_web_search_respects_max_results(monkeypatch, ctx):
    ddg = "".join(
        f'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fe{i}.com">'
        f'T{i}</a><a class="result__snippet" href="x">s{i}</a>' for i in range(10))
    _patch_http(monkeypatch, ddg.encode())
    out = w.web_search(ctx, "anything", max_results=3)
    assert out["provider"] == "duckduckgo"
    assert len(out["results"]) == 3


def test_web_search_unknown_provider_errors(monkeypatch, ctx):
    monkeypatch.setitem(ctx.ws.config, "web", {"provider": "nope"})
    out = w.web_search(ctx, "q")
    assert "error" in out and "unknown" in out["error"]


# ---- fetch ---------------------------------------------------------------------


def test_web_fetch_rejects_non_http_scheme(ctx):
    assert "error" in w.web_fetch(ctx, "file:///etc/passwd")
    assert "error" in w.web_fetch(ctx, "ftp://host/x")


def test_web_fetch_returns_readable_text(monkeypatch, ctx):
    page = b"<html><head><title>Doc</title></head><body><p>Hello world</p></body></html>"
    _patch_http(monkeypatch, page)
    out = w.web_fetch(ctx, "https://example.com/doc", max_chars=1000)
    assert out["title"] == "Doc"
    assert out["text"] == "Hello world"
    assert out["truncated"] is False


def test_web_fetch_truncates(monkeypatch, ctx):
    page = b"<body>" + b"x " * 5000 + b"</body>"
    _patch_http(monkeypatch, page)
    out = w.web_fetch(ctx, "https://example.com", max_chars=500)
    assert out["truncated"] is True and len(out["text"]) == 500


# ---- research delegation -------------------------------------------------------


class _FakeGateway:
    available = True
    model = "fake"
    ref = "fake:test"

    def __init__(self, script):
        self.script = list(script)

    def complete(self, system, transcript, tools, audit_items=None):
        return self.script.pop(0)


def test_research_requires_a_gateway(ctx):
    ctx.gateway = None
    assert "error" in w.research(ctx, "anything")


def test_research_delegates_and_returns_answer(ctx):
    from chipchamp.agent.providers.base import ModelResponse
    ctx.gateway = _FakeGateway(
        [ModelResponse(text="Answer: X is 42 (https://src.example).",
                       tool_calls=[], stop_reason="end_turn")])
    out = w.research(ctx, "what is X?", max_steps=3)
    assert "42" in out["answer"] and out["steps"] == 1


def test_research_tool_cannot_recurse_into_itself():
    # the sub-agent's tool set excludes `research` (no infinite delegation)
    assert "research" not in w.RESEARCHER_TOOLS
