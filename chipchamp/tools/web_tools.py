"""Web research tools (SPEC §9): search + fetch, and a delegated search agent.

Chipchamp can reach the internet to *read* information — a datasheet, a standard's
errata, a tool's docs — but never to download or execute anything. Three tools:

* ``web.search`` — keyword search. Keyless DuckDuckGo by default; a configured
  ``[web]`` provider (Brave / Tavily / Serper) with an API key otherwise.
* ``web.fetch`` — HTTP(S) GET one URL and return its readable text. The body is
  read into memory, size-capped and returned; nothing is written to disk.
* ``research`` — delegate an open question to a bounded **web-researcher**
  sub-agent (search + fetch + read-only repo access) that returns a synthesized,
  source-cited answer.

Built on urllib (no new dependency), matching the provider layer. Configure an
API backend in the workspace config::

    [web]
    provider = "brave"        # brave | tavily | serper | duckduckgo (default)
    api_key_env = "BRAVE_API_KEY"   # or api_key = "…"
    max_results = 5
    timeout = 15
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request

from .base import tool, truncate
from .context import ToolContext

# A real browser UA: many sites (and DuckDuckGo's scrape endpoint) serve a
# bot-challenge page to non-browser agents, which would leave web.fetch/search
# empty. We only ever GET/POST to read — never to download or act.
_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
_MAX_BYTES = 2_000_000      # cap the in-memory read — fetch info, don't hoard files
_DEFAULT_TIMEOUT = 15.0

# The web-researcher sub-agent's least-privilege tool set + brief. Shared with
# the orchestrator role of the same name (agent/roles.py) so the delegated
# agent is identical however it is spawned.
RESEARCHER_TOOLS = {"web.search", "web.fetch", "fs.read", "fs.list", "fs.grep"}
RESEARCHER_SUFFIX = (
    "You are a web-research sub-agent. Answer the question using web.search to "
    "find sources and web.fetch to read them (you may also read the repo with "
    "fs.read/fs.list/fs.grep for context). You may ONLY read — never edit, "
    "submit jobs, or download files. Prefer primary sources; corroborate a "
    "claim across two when it matters. Finish with a concise answer that cites "
    "each supporting URL inline, and say plainly when the sources don't settle "
    "the question rather than guessing.")


# ---- config / http -------------------------------------------------------------


def _web_cfg(ctx: ToolContext) -> dict:
    return (ctx.ws.config.get("web", {}) or {}) if hasattr(ctx, "ws") else {}


def _timeout(ctx: ToolContext) -> float:
    try:
        return float(_web_cfg(ctx).get("timeout") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


def _api_key(cfg: dict) -> str:
    if cfg.get("api_key"):
        return str(cfg["api_key"])
    env = cfg.get("api_key_env")
    return os.environ.get(str(env), "") if env else ""


def _check_url(url: str) -> str:
    """Return an error string for a URL we won't fetch, else ""."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return f"only http/https URLs are fetchable (got '{parsed.scheme or '?'}')"
    if not parsed.netloc:
        return "URL has no host"
    return ""


def _http(url: str, timeout: float, *, data: bytes | None = None,
          headers: dict | None = None) -> tuple[int, str, bytes]:
    """One GET/POST. Returns (status, content_type, body_bytes ≤ _MAX_BYTES)."""
    h = {"User-Agent": _UA,
         "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "") or ""
        raw = r.read(_MAX_BYTES + 1)
        status = getattr(r, "status", 200) or 200
    return status, ctype, raw[:_MAX_BYTES]


def _decode(raw: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    enc = m.group(1) if m else "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ---- html -> readable text -----------------------------------------------------


def _title(page: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def _html_to_text(page: str) -> str:
    # drop <head> (title/meta are surfaced separately) and non-content elements
    page = re.sub(r"(?is)<head\b.*?</head>", " ", page)
    page = re.sub(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\s*>", "\n", page)
    text = re.sub(r"(?s)<[^>]+>", " ", page)
    text = html.unescape(text)
    text = re.sub(r"[ \t\f\v\r]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"(?s)<[^>]+>", "", s)).strip()


# ---- search backends -----------------------------------------------------------


def _provider_name(cfg: dict) -> str:
    return str(cfg.get("provider") or "duckduckgo").strip().lower()


def _ddg_unwrap(href: str) -> str:
    """DuckDuckGo wraps targets as //duckduckgo.com/l/?uddg=<encoded>."""
    if "uddg=" in href:
        q = urllib.parse.urlparse(href).query
        val = urllib.parse.parse_qs(q).get("uddg")
        if val:
            return urllib.parse.unquote(val[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def _ddg_scrape(query: str, n: int, timeout: float) -> list[dict]:
    """Scrape DuckDuckGo's HTML endpoint for real web results. POST (the form
    the page submits) + a browser UA get past the simple bot gate on a normal
    connection; a datacenter IP may still be challenged (→ empty, caller falls
    back)."""
    data = urllib.parse.urlencode({"q": query}).encode()
    _, ctype, raw = _http("https://html.duckduckgo.com/html/", timeout, data=data,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    page = _decode(raw, ctype)
    hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                       page, re.I | re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.I | re.S)
    out: list[dict] = []
    for i, (href, title) in enumerate(hrefs[:n]):
        out.append({"title": _strip_tags(title),
                    "url": _ddg_unwrap(html.unescape(href)),
                    "snippet": _strip_tags(snips[i]) if i < len(snips) else ""})
    return out


def _ddg_instant(query: str, n: int, timeout: float) -> list[dict]:
    """Keyless fallback: DuckDuckGo's Instant Answer JSON API. Always returns
    JSON (no bot gate) but only covers curated topics, so it's a backstop for
    the scrape, not a replacement."""
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": 1, "no_redirect": 1,
         "t": "chipchamp"})
    _, ctype, raw = _http(url, timeout, headers={"Accept": "application/json"})
    data = json.loads(_decode(raw, ctype) or "{}")
    out: list[dict] = []
    if data.get("AbstractURL"):
        out.append({"title": data.get("Heading", "") or query,
                    "url": data["AbstractURL"],
                    "snippet": data.get("AbstractText", "")})

    def walk(topics):
        for t in topics:
            if len(out) >= n:
                return
            if isinstance(t, dict) and t.get("Topics"):
                walk(t["Topics"])
            elif isinstance(t, dict) and t.get("FirstURL"):
                out.append({"title": (t.get("Text", "") or "").split(" - ")[0],
                            "url": t["FirstURL"], "snippet": t.get("Text", "")})

    walk(data.get("RelatedTopics") or [])
    return out[:n]


def _ddg_search(query: str, n: int, timeout: float) -> list[dict]:
    results = _ddg_scrape(query, n, timeout)
    if results:
        return results
    try:
        return _ddg_instant(query, n, timeout)  # keyless backstop
    except Exception:
        return []


def _brave_search(cfg: dict, query: str, n: int, timeout: float) -> list[dict]:
    key = _api_key(cfg)
    if not key:
        raise RuntimeError("brave search needs [web] api_key / api_key_env")
    base = str(cfg.get("base_url") or "https://api.search.brave.com/res/v1/web/search")
    url = base + "?" + urllib.parse.urlencode({"q": query, "count": n})
    _, ctype, raw = _http(url, timeout,
                          headers={"X-Subscription-Token": key, "Accept": "application/json"})
    data = json.loads(_decode(raw, ctype) or "{}")
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": _strip_tags(r.get("description", ""))}
            for r in ((data.get("web") or {}).get("results") or [])[:n]]


def _tavily_search(cfg: dict, query: str, n: int, timeout: float) -> list[dict]:
    key = _api_key(cfg)
    if not key:
        raise RuntimeError("tavily search needs [web] api_key / api_key_env")
    base = str(cfg.get("base_url") or "https://api.tavily.com/search")
    body = json.dumps({"api_key": key, "query": query, "max_results": n}).encode()
    _, ctype, raw = _http(base, timeout, data=body,
                          headers={"Content-Type": "application/json"})
    data = json.loads(_decode(raw, ctype) or "{}")
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": _strip_tags(r.get("content", ""))}
            for r in (data.get("results") or [])[:n]]


def _serper_search(cfg: dict, query: str, n: int, timeout: float) -> list[dict]:
    key = _api_key(cfg)
    if not key:
        raise RuntimeError("serper search needs [web] api_key / api_key_env")
    base = str(cfg.get("base_url") or "https://google.serper.dev/search")
    body = json.dumps({"q": query, "num": n}).encode()
    _, ctype, raw = _http(base, timeout, data=body,
                          headers={"X-API-KEY": key, "Content-Type": "application/json"})
    data = json.loads(_decode(raw, ctype) or "{}")
    return [{"title": r.get("title", ""), "url": r.get("link", ""),
             "snippet": _strip_tags(r.get("snippet", ""))}
            for r in (data.get("organic") or [])[:n]]


_BACKENDS = {"brave": _brave_search, "tavily": _tavily_search, "serper": _serper_search}


def _do_search(cfg: dict, query: str, n: int, timeout: float) -> list[dict]:
    name = _provider_name(cfg)
    if name in ("duckduckgo", "ddg", ""):
        return _ddg_search(query, n, timeout)
    backend = _BACKENDS.get(name)
    if backend is None:
        raise RuntimeError(f"unknown [web] provider '{name}' "
                           f"(use one of: duckduckgo, {', '.join(_BACKENDS)})")
    return backend(cfg, query, n, timeout)


# ---- tools ---------------------------------------------------------------------


@tool("web.search",
      "Search the web and return ranked {title, url, snippet} results "
      "(DuckDuckGo by default; a configured [web] provider otherwise). Then read "
      "a result with web.fetch. Read-only — never downloads.",
      cost="cheap", permission="read", group="web",
      schema={"type": "object", "properties": {
          "query": {"type": "string"},
          "max_results": {"type": "integer", "default": 5}},
          "required": ["query"]})
def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> dict:
    n = max(1, min(int(max_results or 5), 20))
    cfg = _web_cfg(ctx)
    provider = _provider_name(cfg)
    try:
        results = _do_search(cfg, query, n, _timeout(ctx))
    except Exception as e:
        return {"error": f"search failed: {type(e).__name__}: {e}"}
    note = ""
    if not results and provider in ("duckduckgo", "ddg", ""):
        note = ("no results — keyless DuckDuckGo can be rate-limited/blocked "
                "(esp. from a datacenter IP). For reliable search configure a "
                "[web] provider = brave|tavily|serper with api_key/api_key_env. "
                "web.fetch still works for any URL you already have.")
    return truncate({"query": query, "provider": provider,
                     "results": results[:n], "note": note})


@tool("web.fetch",
      "Fetch one http(s) URL and return its readable text (HTML stripped to "
      "prose). Reads into memory and returns it — nothing is written to disk. "
      "Use to read a page/spec/datasheet you have a URL for.",
      cost="cheap", permission="read", group="web",
      schema={"type": "object", "properties": {
          "url": {"type": "string"},
          "max_chars": {"type": "integer", "default": 8000,
                        "description": "cap on returned text (default 8000)"}},
          "required": ["url"]})
def web_fetch(ctx: ToolContext, url: str, max_chars: int = 8000) -> dict:
    err = _check_url(url)
    if err:
        return {"error": err}
    cap = max(500, min(int(max_chars or 8000), 40000))
    try:
        status, ctype, raw = _http(url, _timeout(ctx))
    except Exception as e:
        return {"error": f"fetch failed: {type(e).__name__}: {e}"}
    body = _decode(raw, ctype)
    is_html = "html" in ctype.lower() or body.lstrip()[:1] == "<"
    title = _title(body) if is_html else ""
    text = _html_to_text(body) if is_html else body
    return truncate({"url": url, "status": status, "content_type": ctype,
                     "title": title, "bytes": len(raw),
                     "truncated": len(text) > cap, "text": text[:cap]},
                    max_str=cap + 200)


@tool("research",
      "Delegate an open question to a web-research sub-agent that browses the "
      "internet (search + fetch, read-only, no downloads) and returns a "
      "synthesized, source-cited answer. Use for questions needing current "
      "external info: standards, errata, tool/version docs, vendor datasheets.",
      cost="metered", permission="read", group="web",
      schema={"type": "object", "properties": {
          "question": {"type": "string"},
          "max_steps": {"type": "integer", "default": 8,
                        "description": "sub-agent step budget (default 8)"}},
          "required": ["question"]})
def research(ctx: ToolContext, question: str, max_steps: int = 8) -> dict:
    gw = getattr(ctx, "gateway", None)
    if gw is None or not getattr(gw, "available", False):
        return {"error": "no model available to run the research sub-agent"}
    from ..agent.loop import AgentLoop  # lazy: avoids a tools<->agent import cycle
    from .catalog import all_tools
    sub_tools = {n: t for n, t in all_tools().items() if n in RESEARCHER_TOOLS}
    # isolate the sub-agent's task state but share job store + policy (FR-MA-02)
    child = ToolContext(ctx.ws, target=ctx.target_name,
                        runner=ctx.runner, policy=ctx.policy)
    child.gateway = gw
    steps = max(1, min(int(max_steps or 8), 20))
    sub = AgentLoop(child, gw, max_steps=steps, tools=sub_tools,
                    system_suffix=RESEARCHER_SUFFIX)
    try:
        out = sub.run(question)
    except Exception as e:
        return {"error": f"research sub-agent failed: {type(e).__name__}: {e}"}
    return truncate({"question": question, "answer": out.get("text", ""),
                     "steps": out.get("steps", 0)})
