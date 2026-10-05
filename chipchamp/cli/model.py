"""chipchamp CLI — model commands (split from the one-file cli.py)."""
from __future__ import annotations

import json
import sys

import click

from .. import brand as _brand

try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _ctx, cli


def _build_gateway(c, model_override=None, provider_override=None, role=None):
    """Resolve provider+model from the registry (selection file > env > config).
    `role` consults [model.routing] (§14) — e.g. subagents on a cheaper model."""
    from ..agent import ModelGateway, ModelRegistry
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    audit = str(c.ws.dot / "audit.jsonl")
    if provider_override:
        prov = reg.provider(provider_override)
        model = model_override or (prov.config.default_model if prov else "")
        reason = "ok" if prov else f"unknown provider '{provider_override}'"
    else:
        prov, model, reason = reg.resolve(role=role)
        model = model_override or model
    if prov is None:
        return None, reason, audit
    mcfg = c.ws.config.get("model", {}) or {}
    # 16K output ceiling by default: enough for a reasoning model to plan AND
    # emit a full RTL module in one turn (4K was too low — it burned the budget
    # on reasoning and truncated), but bounded so a single call can't run away
    # to a huge generation and blow the per-call wall-clock timeout on a slow
    # local model. If a module needs more, the loop's length-stop nudge
    # continues it across turns. Override: [model] max_tokens / CHIPCHAMP_MODEL_MAX_TOKENS.
    from ..brand import env as _benv
    max_tokens = int(_benv("MODEL_MAX_TOKENS", 0) or
                     mcfg.get("max_tokens", 0) or 16384)
    # Reasoning effort (`/effort`): explicit choice > env > config, normalized
    # and GATED per model by resolve_effort — the single decision point shared
    # with the router's hot-swap path, so a saved effort is never sent to a
    # model that would reject the parameter.
    from ..agent.effort import resolve_effort
    ref = f"{prov.name}:{model}"
    effort = resolve_effort(c.ws.dot, ref, c.ws.config)
    # `/model tune`: per-model generation options threaded onto the gateway the
    # same way effort is, so the wire request carries them from the first call.
    from ..agent.model_opts import resolve_opts
    options = resolve_opts(c.ws.dot, ref, c.ws.config)
    # `/timeout`: per-model call budget (per-ref > global > config > provider
    # default) — a slow model gets a bigger budget without touching the fast one.
    from ..agent.model_timeout import resolve_timeout
    timeout = resolve_timeout(c.ws.dot, ref, c.ws.config)
    return ModelGateway(prov, model, audit_log=audit, timeout=timeout,
                        max_tokens=max_tokens, reasoning_effort=effort,
                        options=options), "ok", audit


def _build_router(c):
    """A ModelRouter loaded from .chipchamp/router.json (+ capability profiles
    from config). Always returned — a no-op while disabled — so `/router` can
    toggle it live on the running loop. None only if the agent module is broken."""
    try:
        from ..agent import ModelRegistry
        from ..agent.router import ModelRouter, RouterConfig
        reg = ModelRegistry(c.ws.config, str(c.ws.dot))
        return ModelRouter(reg, RouterConfig.load(c.ws.dot), c.ws.config)
    except Exception:
        return None


def _router_status(c, r) -> None:
    from ..agent.model_profiles import CAPABILITIES
    cfg = r.cfg
    echo("[bold]router[/] " + ("[green]on[/]" if cfg.enabled else "[yellow]off[/]")
         + "   layers: " + " ".join(f"{k}{'✓' if v else '✗'}" for k, v in cfg.layers.items())
         + "   triggers: " + " ".join(f"{k}{'✓' if v else '✗'}" for k, v in cfg.triggers.items()))
    if cfg.pool:
        echo("  pool (ranked strengths):")
        for ref in cfg.pool:
            p = r._profile(ref)
            best = max([c2 for c2 in CAPABILITIES if c2 != "speed"],
                       key=lambda cap: p.score(cap))
            warn = f" · [red]⚠ {','.join(p.failure_modes)}[/]" if p.failure_modes else ""
            echo(f"    [bold]{ref}[/]  [dim]best: {best} {p.score(best):.2f}"
                 f" · speed {p.score('speed'):.2f}[/]{warn}")
    else:
        echo("  pool: [dim](empty) — `/router pool add <provider:model>`[/]")
    for cap, ch in (cfg.chains or {}).items():
        echo(f"  [dim]chain {cap}:[/] {' → '.join(ch)}")


_EFFORT_BLURB = {
    "minimal": "barely thinks · fastest",
    "low": "shallow search · fastest turnaround",
    "medium": "balanced",
    "high": "deep search · longest runtime",
}


def _effort_wave(idx: int, n: int, width: int = 22) -> str:
    """A timing-diagram pulse whose HIGH window is the model's think time.

    Effort is the reasoning analogue of a synthesis effort level: a wider think
    window buys a better result and costs runtime. Rendered as a waveform so the
    trade reads at a glance (`___┌─────┐____`)."""
    hi = max(1, round((idx + 1) / n * (width - 5)))
    return "__┌" + "─" * hi + "┐" + "_" * max(0, width - 4 - hi)


def _effort_status(ref: str, levels, cur: str, sent: str) -> None:
    """Render the lever. `cur` is the persisted choice; `sent` is what
    resolve_effort actually puts on the wire for this model — the display must
    reflect the wire, not the file."""
    echo(f"[bold]⚙ reasoning effort[/] [dim]· {ref or '(no model selected)'}[/]")
    if not levels:
        echo("  [yellow]this model exposes no reasoning-effort control[/]"
             " [dim](not a reasoning model)[/]")
        echo("  [dim]built-in: gpt-oss · gpt-5 · o-series — extend via config "
             "\\[model.effort_support][/]")
        if cur == "off":
            echo("  [dim]effort pinned [bold]off[/] (/effort auto to unpin)[/]")
        elif cur and sent == cur:
            echo(f"  [green]saved effort [bold]{cur}[/] was forced on this "
                 f"model and IS sent[/]")
        elif cur:
            echo(f"  [dim]a saved effort ([bold]{cur}[/]) is set but not sent "
                 f"to this model (it was set on a different one)[/]")
        return
    echo("  [dim]think window · wider = deeper search, longer runtime[/]")
    for i, lv in enumerate(levels):
        active = (lv == sent)
        mark = "[green]▸[/]" if active else " "
        name = f"[bold green]{lv:<8}[/]" if active else f"[dim]{lv:<8}[/]"
        wave = _effort_wave(i, len(levels))
        wave = f"[green]{wave}[/]" if active else f"[grey37]{wave}[/]"
        tail = f"[dim]{_EFFORT_BLURB.get(lv, '')}[/]"
        echo(f"  {mark} {name} {wave}  {tail}"
             + ("  [green]← active[/]" if active else ""))
    if cur == "off":
        echo("  [dim]pinned [bold]off[/] — no field sent (/effort auto to unpin)[/]")
    elif not sent:
        echo("  [dim]unset — the model's own default is used (no field sent)[/]")
    elif not cur:
        echo(f"  [dim]{sent} comes from env/config "
             f"({_brand.env_name('MODEL_REASONING_EFFORT')} / [model] reasoning_effort)[/]")
    echo(f"  [dim]/effort <{('|'.join(levels))}> · /effort off · /effort auto[/]")


def _effort_cmd(c, args, loop=None) -> None:
    """Shared handler for `/effort`: show or set the reasoning-effort lever.

    Persists to .chipchamp/effort.json and retunes the live gateway so the next
    model call uses it (no restart)."""
    from ..agent.effort import (LEVELS, load_effort, resolve_effort, save_effort,
                               supported_levels)
    ref = getattr(getattr(loop, "gateway", None), "ref", "") or ""
    if not ref:
        try:
            from ..agent import ModelRegistry
            sel = ModelRegistry(c.ws.config, str(c.ws.dot)).current() or {}
            if sel.get("provider"):
                ref = f"{sel['provider']}:{sel.get('model', '')}"
        except Exception:
            ref = ""
    levels = supported_levels(ref, c.ws.config)
    cur = load_effort(c.ws.dot)
    if not args:
        _effort_status(ref, levels, cur, resolve_effort(c.ws.dot, ref, c.ws.config))
        return
    a = args[0].strip().lower()
    if a in ("off", "auto", "default", "clear", "none"):
        # "off" pins effort off (persisted, masks env/config defaults); the
        # other synonyms reset to unset, letting env/config apply again.
        pin = a == "off"
        save_effort(c.ws.dot, "off" if pin else "")
        if loop is not None and getattr(loop, "gateway", None) is not None:
            loop.gateway.reasoning_effort = ""
        if pin:
            echo("[green]effort off — pinned[/] [dim](no field sent, masks any "
                 "env/config default; /effort auto to unpin)[/]")
        else:
            echo("[green]effort reset[/] [dim]— env/config default applies if "
                 "set, else the model's own default[/]")
        return
    valid = levels or LEVELS
    if a not in valid:
        echo(f"[yellow]unknown level '{a}'[/] [dim]— try: {' · '.join(valid)}[/]")
        return
    if levels is None:
        echo(f"[yellow]⚠ {ref or 'this model'} is not known to support "
             f"reasoning_effort[/] [dim]— forcing it for THIS model only; it "
             f"may be ignored or rejected. Declare support via config "
             f"\\[model.effort_support] to make it first-class.[/]")
    save_effort(c.ws.dot, a, ref=ref)
    if loop is not None and getattr(loop, "gateway", None) is not None:
        loop.gateway.reasoning_effort = a
    echo(f"[green]effort → {a}[/] [dim](saved to .chipchamp/effort.json)[/]")
    _effort_status(ref, levels, a, resolve_effort(c.ws.dot, ref, c.ws.config))


def _current_ref(c, loop=None) -> str:
    """The model ref `/model tune` should target: the live gateway's model when
    one is running, else the selection the next gateway would resolve to."""
    gw = getattr(loop, "gateway", None) if loop is not None else None
    ref = getattr(gw, "ref", "") if gw is not None else ""
    if ref and not ref.startswith("offline"):
        return ref
    try:
        from ..agent import ModelRegistry
        reg = ModelRegistry(c.ws.config, str(c.ws.dot))
        prov, model, _ = reg.resolve()
        return f"{prov.name}:{model}" if prov else ""
    except Exception:
        return ""


# Reference of generation parameters commonly accepted by local servers, shown
# by `/model tune --help` (and the `chipchamp model --help` epilog). chipchamp
# sends whatever you set VERBATIM, so each server's own docs are authoritative —
# this is the practical shortlist. (name, default, description).
_TUNE_GROUPS = [
    ("sampling — Ollama + LM Studio", [
        ("temperature", "0.8", "randomness; lower = more deterministic"),
        ("top_p", "0.9", "nucleus sampling — keep tokens summing to p"),
        ("top_k", "40", "sample only from the K most-likely tokens"),
        ("min_p", "0.0", "drop tokens below this fraction of the top token"),
        ("repeat_penalty", "1.1", "penalize repetition (Ollama · LM Studio)"),
        ("presence_penalty", "0.0", "OpenAI-style: penalize already-seen tokens"),
        ("frequency_penalty", "0.0", "OpenAI-style: penalize frequent tokens"),
        ("seed", "0", "fix for reproducible output"),
        ("stop", '["</s>"]', "stop sequence(s) — JSON list or string"),
        ("max_tokens", "—", "cap tokens generated this call"),
    ]),
    ("Ollama — context & runtime", [
        ("num_ctx", "4096", "context-window / KV-cache size, in tokens"),
        ("num_predict", "-1", "max tokens to generate (-1 = until stop/full)"),
        ("num_keep", "0", "prompt tokens kept when the context overflows"),
        ("repeat_last_n", "64", "how far back repeat_penalty looks"),
        ("mirostat", "0", "0 off · 1 Mirostat · 2 Mirostat 2.0"),
        ("mirostat_tau", "5.0", "Mirostat target entropy"),
        ("mirostat_eta", "0.1", "Mirostat learning rate"),
        ("num_gpu", "—", "layers to offload to the GPU (0 = CPU only)"),
        ("num_thread", "—", "CPU threads to use"),
        ("num_batch", "512", "prompt batch size"),
    ]),
    ("LM Studio — extras", [
        ("response_format", '{"type":"json_object"}', "constrain output (JSON / schema)"),
        ("logit_bias", '{"123":-100}', "per-token-id bias map"),
        ("ttl", "—", "seconds to keep the model loaded (JIT auto-unload)"),
    ]),
]


def _model_tune_help() -> None:
    """`/model tune --help`: the tunable-parameter reference for local servers."""
    echo("[bold]🎛 /model tune[/] — set generation parameters, sent "
         "[bold]verbatim[/] to your model server")
    echo("  [dim]/model tune <key>=<value> …  ·  --global applies to every model  "
         "·  <key> alone (or key=) clears it  ·  /model tune reset[/]")
    echo("  [dim]values are JSON-typed: 8192→int · 0.7→float · true→bool · "
         r'["</s>"]→list[/]')
    for title, params in _TUNE_GROUPS:
        echo(f"[bold]{title}[/]")
        for name, default, desc in params:
            d = f" [dim](default {default})[/]" if default not in ("—", "") else ""
            echo(f"  [green]{name:<17}[/] {desc}{d}")
    echo("[dim]Commonly-supported keys — your server's docs are the authoritative "
         "list. Unknown keys are sent as-is; a server may ignore or reject one.[/]")


def _tune_epilog() -> str:
    """Plain-text version of the reference for the `chipchamp model` help screen.
    Each paragraph starts with \\b so Click preserves its layout (a single \\b
    would protect only the first paragraph, letting the rest re-wrap)."""
    paras = ["\b\nTunable parameters (--tune KEY=VALUE) — sent verbatim to the "
             "server:"]
    for title, params in _TUNE_GROUPS:
        rows = [f"{title}:"]
        for name, default, desc in params:
            d = f"  (default {default})" if default not in ("—", "") else ""
            rows.append(f"  {name:<17} {desc}{d}")
        paras.append("\b\n" + "\n".join(rows))
    paras.append("Values are JSON-typed. Your server's docs are the authoritative "
                 "list; unknown keys are sent as-is.")
    return "\n\n".join(paras)


def _model_tune_status(c, ref: str) -> None:
    from ..agent.model_opts import GLOBAL, load_state, resolve_opts
    eff = resolve_opts(c.ws.dot, ref, c.ws.config)
    st = load_state(c.ws.dot)
    echo(f"[bold]🎛 model tuning[/] [dim]· {ref or '(no model selected)'}[/]")
    if not eff:
        echo("  [dim]no options set — the model runs on provider defaults[/]")
    else:
        for k, v in sorted(eff.items()):
            echo(f"  [green]{k}[/] = {json.dumps(v)}")
    glob = st.get(GLOBAL, {}) or {}
    if glob and ref not in ("", GLOBAL):
        echo("  [dim]incl. global (*): "
             + ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(glob.items()))
             + "[/]")
    echo(r"  [dim]/model tune <key>=<value> … · /model tune \[--global] … · "
         r"/model tune <key> (clear one) · /model tune reset[/]")
    echo("  [dim]sent verbatim in the chat request — [bold]/model tune --help[/] "
         "lists the Ollama / LM Studio parameters you can set[/]")


def _model_tune_cmd(c, args, loop=None) -> None:
    """Shared handler for `/model tune …` (live) and `chipchamp model --tune`
    (persisted to .chipchamp/model_opts.json)."""
    from ..agent.model_opts import parse_kv, reset, resolve_opts, set_opts
    if args and args[0] in ("--help", "-h", "help", "?"):
        _model_tune_help()
        return
    glob = False
    if args and args[0] in ("--global", "-g", "global"):
        glob, args = True, args[1:]
    ref = _current_ref(c, loop)
    if not args:
        _model_tune_status(c, ref)
        return
    if args[0] in ("reset", "clear"):
        reset(c.ws.dot, ref, glob=glob)
        echo(f"[green]tuning reset[/] [dim]({'global (*)' if glob else ref})[/]")
    else:
        if not glob and not ref:
            echo("[yellow]no model selected[/] — pick one with [bold]/model[/], "
                 "or tune every model with [bold]/model tune --global …[/]")
            return
        setk, unset = parse_kv(args)
        set_opts(c.ws.dot, ref, setk, unset, glob=glob)
        where = "global (*)" if glob else ref
        if setk:
            echo("[green]tuned[/] "
                 + ", ".join(f"{k}={json.dumps(v)}" for k, v in setk.items())
                 + f" [dim]({where})[/]")
        if unset:
            echo(f"[green]cleared[/] {', '.join(unset)} [dim]({where})[/]")
    # retune the live gateway so the change takes effect on the next call
    if loop is not None and getattr(loop, "gateway", None) is not None:
        try:
            loop.gateway.options = resolve_opts(c.ws.dot, loop.gateway.ref,
                                                c.ws.config)
        except Exception:
            pass
    _model_tune_status(c, ref)


def _fmt_secs(s) -> str:
    if s is None:
        return "provider default"
    if s <= 0:
        return "off (no wall-clock limit)"
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)}m ({int(s)}s)"
    return f"{int(s)}s"


def _parse_duration(tok: str):
    """'1200'|'20m'|'300s' → float seconds; 'off'/'none'/'0' → 0.0; else None."""
    import re as _re
    t = (tok or "").strip().lower()
    if t in ("off", "none", "0"):
        return 0.0
    m = _re.match(r"^(\d+(?:\.\d+)?)(s|m|)$", t)
    if not m:
        return None
    v = float(m.group(1))
    return v * 60 if m.group(2) == "m" else v


def _retune_gateway_timeout(loop, seconds) -> None:
    """Apply a resolved budget to the live gateway + its provider socket. None
    means fall back to the provider default."""
    gw = getattr(loop, "gateway", None) if loop is not None else None
    if gw is None:
        return
    try:
        from ..agent.providers.base import DEFAULT_REQUEST_TIMEOUT
        val = DEFAULT_REQUEST_TIMEOUT if seconds is None else float(seconds)
        gw.timeout = val
        if getattr(gw, "provider", None) is not None:
            gw.provider.request_timeout = val
    except Exception:
        pass


def _timeout_status(c, ref: str, loop=None) -> None:
    from ..agent.model_timeout import GLOBAL, load_state, resolve_timeout
    eff = resolve_timeout(c.ws.dot, ref, c.ws.config)
    st = load_state(c.ws.dot)
    echo(f"[bold]⏱ model timeout[/] [dim]· {ref or '(no model selected)'}[/]")
    echo(f"  per-call budget: [green]{_fmt_secs(eff)}[/]")
    if ref and ref in st:
        src = "this model"
    elif GLOBAL in st:
        src = "global (*)"
    elif ((c.ws.config.get("model", {}) or {}).get("request_timeout")) is not None:
        src = "[model] request_timeout"
    else:
        src = f"default ({_brand.env_name('MODEL_TIMEOUT')} or 600s)"
    echo(f"  [dim]source: {src}[/]")
    echo(r"  [dim]/timeout <seconds|Nm|off> · /timeout \[--global] <n> · "
         r"/timeout reset[/]")
    echo("  [dim]while streaming this is the max GAP between tokens, not the whole "
         "turn — a model that keeps generating never times out[/]")


def _timeout_cmd(c, args, loop=None) -> None:
    """Shared handler for `/timeout …` (live) and `chipchamp model --timeout`
    (per-model, persisted to .chipchamp/timeout.json)."""
    from ..agent.model_timeout import reset, resolve_timeout, set_timeout
    if args and args[0] in ("--help", "-h", "help", "?"):
        echo("[bold]⏱ /timeout[/] — per-model call budget (wall-clock seconds)")
        echo("  [dim]/timeout 1200 · /timeout 20m · /timeout off · /timeout reset "
             r"· /timeout \[--global] 900[/]")
        echo("  [dim]raise it for a slow model without touching the fast ones. "
             "While streaming it bounds the GAP between tokens, so a steadily-"
             "generating model runs as long as it needs.[/]")
        return
    glob = False
    if args and args[0] in ("--global", "-g", "global"):
        glob, args = True, args[1:]
    ref = _current_ref(c, loop)
    if not args or args[0] in ("show", "status"):
        _timeout_status(c, ref, loop)
        return
    if args[0] in ("reset", "auto", "default", "clear"):
        reset(c.ws.dot, ref, glob=glob)
        echo(f"[green]timeout reset[/] [dim]({'global (*)' if glob else ref})[/]")
    else:
        secs = _parse_duration(args[0])
        if secs is None:
            echo(f"[yellow]not a duration:[/] {args[0]} [dim]— give seconds "
                 "(1200), minutes (20m), or off[/]")
            return
        if not glob and not ref:
            echo("[yellow]no model selected[/] — pick one with [bold]/model[/], "
                 "or set a default with [bold]/timeout --global <n>[/]")
            return
        set_timeout(c.ws.dot, ref, secs, glob=glob)
        echo(f"[green]timeout → {_fmt_secs(secs)}[/] "
             f"[dim]({'global (*)' if glob else ref})[/]")
    if loop is not None and getattr(loop, "gateway", None) is not None:
        _retune_gateway_timeout(
            loop, resolve_timeout(c.ws.dot, loop.gateway.ref, c.ws.config))
    _timeout_status(c, ref, loop)


def _offer_timeout_retry(c, loop, line, out) -> str | None:
    """After a model-call timeout, offer a bigger-budget retry (interactive
    only). Returns 'retry' when the caller should re-run the same input."""
    if not sys.stdin.isatty():
        return None  # headless/piped: the error already surfaced; don't block
    ref = out.get("ref") or (getattr(loop, "gateway", None) and loop.gateway.ref) or ""
    cur = out.get("timeout") or 600.0
    nxt = int(cur * 2)
    echo(f"[yellow]⏱ {ref} timed out after {_fmt_secs(cur)}.[/]")
    echo(rf"  [bold]\[r][/] retry with {_fmt_secs(nxt)}   "
         rf"[bold]\[m][/] switch model   [bold]\[enter][/] give up   "
         r"[dim]· /router on auto-switches next time[/]")
    try:
        ans = input("  » ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        echo("")
        return None
    if ans == "m":
        _select_model(c)
        gw2, _, _ = _build_gateway(c)
        if gw2:
            loop.gateway = gw2
            echo(f"[green]switched to {gw2.ref}[/] — resend your request")
        return None
    if ans != "r":
        return None
    from ..agent.model_timeout import set_timeout
    if ref:
        set_timeout(c.ws.dot, ref, nxt)  # remember this model is slow
    _retune_gateway_timeout(loop, nxt)
    echo(f"[dim]retrying with {_fmt_secs(nxt)}"
         + (f" (saved for {ref})" if ref else "") + "…[/]")
    return "retry"


def _fetch_server_config(pc, model: str) -> dict:
    """Query a local server for a model's own settings. Ollama exposes the
    Modelfile PARAMETERs via /api/show; LM Studio exposes model info via its
    native /api/v0. Best-effort — returns {'error': …} when unreachable."""
    import json as _json
    import urllib.parse as _up
    import urllib.request as _rq
    base = (getattr(pc, "base_url", "") or "").rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base  # native APIs
    name = (getattr(pc, "name", "") or "").lower()
    hdr = {"Content-Type": "application/json"}
    try:
        if "11434" in base or name == "ollama":
            req = _rq.Request(root + "/api/show",
                              data=_json.dumps({"model": model}).encode(),
                              headers=hdr, method="POST")
            with _rq.urlopen(req, timeout=6) as r:
                data = _json.load(r)
            settings: dict = {}
            for ln in (data.get("parameters") or "").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                k, _, v = ln.partition(" ")
                settings.setdefault(k, []).append(v.strip())     # `stop` repeats
            flat = {k: (v[0] if len(v) == 1 else v) for k, v in settings.items()}
            det = data.get("details") or {}
            for k in ("family", "parameter_size", "quantization_level"):
                if det.get(k):
                    flat[k] = det[k]
            mi = data.get("model_info") or {}
            ctx = next((mi[k] for k in mi if k.endswith("context_length")), None)
            if ctx:
                flat.setdefault("context_length (max)", ctx)
            return {"source": "ollama", "settings": flat}
        if "1234" in base or name in ("lmstudio", "lm-studio", "lm_studio"):
            req = _rq.Request(root + "/api/v0/models/" + _up.quote(model),
                              headers=hdr, method="GET")
            with _rq.urlopen(req, timeout=6) as r:
                data = _json.load(r)
            keys = ("type", "arch", "quantization", "state", "max_context_length",
                    "loaded_context_length", "publisher", "compatibility_type")
            flat = {k: data[k] for k in keys if data.get(k) not in (None, "")}
            return {"source": "lm studio", "settings": flat}
        return {"error": f"don't know how to query '{name or base}' "
                         "(supported: ollama, lm studio)"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _show_model_config(c) -> None:
    """`model --showconfig`: the selected model's settings as the server reports
    them, plus the overrides chipchamp layers on top (effort / tune / timeout)."""
    from ..agent import ModelRegistry
    from ..agent.effort import resolve_effort
    from ..agent.model_opts import resolve_opts
    from ..agent.model_timeout import resolve_timeout
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    prov, model, reason = reg.resolve()
    if prov is None or not model:
        echo(f"[yellow]no model selected[/] — pick one with `chipchamp model` "
             f"[dim]({reason})[/]")
        return
    ref = f"{prov.name}:{model}"
    echo(f"[bold]⚙ {ref}[/] [dim]{prov.config.base_url}[/]")
    cfg = _fetch_server_config(prov.config, model)
    if cfg.get("error"):
        echo(f"  [yellow]server settings unavailable:[/] {cfg['error']}")
    else:
        echo(f"  [bold]as configured in {cfg['source']}[/]:")
        if cfg["settings"]:
            for k, v in cfg["settings"].items():
                echo(f"    [green]{k:22}[/] {v}")
        else:
            echo("    [dim](the server exposes no per-parameter defaults)[/]")
    eff = resolve_effort(c.ws.dot, ref, c.ws.config)
    opts = resolve_opts(c.ws.dot, ref, c.ws.config)
    to = resolve_timeout(c.ws.dot, ref, c.ws.config)
    echo("  [bold]chipchamp overrides[/] [dim](sent on top of the above)[/]:")
    echo(f"    [green]{'effort':22}[/] {eff or '(none)'}")
    echo(f"    [green]{'timeout':22}[/] {_fmt_secs(to)}")
    if opts:
        for k, v in sorted(opts.items()):
            echo(f"    [green]{('tune ' + k):22}[/] {json.dumps(v)}")
    else:
        echo(f"    [green]{'tune':22}[/] [dim](none) — /model tune to set[/]")


def _router_cmd(c, args, loop=None) -> None:
    """Shared handler for `/router` (live on the running loop) and
    `chipchamp router …` (transient, persisted to .chipchamp/router.json)."""
    from ..agent import ModelRegistry
    from ..agent.model_profiles import CAPABILITIES, load_profiles
    from ..agent.router import (LAYERS, TRIGGERS, ModelRouter, RouterConfig,
                               telemetry_stats)
    r = getattr(loop, "router", None) if loop is not None else None
    if r is None:
        r = ModelRouter(ModelRegistry(c.ws.config, str(c.ws.dot)),
                        RouterConfig.load(c.ws.dot), c.ws.config)
        if loop is not None:
            loop.router = r
    cfg = r.cfg

    def save():
        cfg.save(c.ws.dot)

    sub = (args[0] if args else "status").lower()
    rest = args[1:]
    if sub in ("status", "st", ""):
        return _router_status(c, r)
    if sub in ("on", "off"):
        cfg.enabled = sub == "on"
        save()
        echo(f"[green]router {sub}[/]")
        return _router_status(c, r) if sub == "on" else None
    if sub == "layer":
        if len(rest) == 2 and rest[0] in LAYERS and rest[1] in ("on", "off"):
            cfg.layers[rest[0]] = rest[1] == "on"
            save()
            return echo(f"[green]layer {rest[0]} → {rest[1]}[/]")
        return echo(f"[yellow]usage: /router layer <{'|'.join(LAYERS)}> on|off[/]")
    if sub in ("trigger", "triggers"):
        if len(rest) == 2 and rest[0] in TRIGGERS and rest[1] in ("on", "off"):
            cfg.triggers[rest[0]] = rest[1] == "on"
            save()
            return echo(f"[green]trigger {rest[0]} → {rest[1]}[/]")
        return echo(f"[yellow]usage: /router triggers <{'|'.join(TRIGGERS)}> on|off[/]")
    if sub == "pool":
        act = rest[0] if rest else "ls"
        if act == "add" and len(rest) >= 2:
            for ref in rest[1:]:
                if ref not in cfg.pool:
                    cfg.pool.append(ref)
            save()
        elif act in ("rm", "remove") and len(rest) >= 2:
            cfg.pool = [x for x in cfg.pool if x not in rest[1:]]
            save()
        elif act != "ls":
            return echo("[yellow]usage: /router pool add|rm <provider:model …> | pool ls[/]")
        return echo("  pool: " + (", ".join(cfg.pool) or "[dim](empty)[/]"))
    if sub == "profile":
        if not rest:
            return echo("[yellow]usage: /router profile <ref> [cap=score …][/]")
        ref = rest[0]
        prof = r._profile(ref)
        if len(rest) == 1:
            echo(f"[bold]{ref}[/] [dim]{prof.note}[/]")
            for cap in CAPABILITIES:
                echo(f"  {cap}: {prof.score(cap):.2f}")
            if prof.failure_modes:
                echo(f"  failure_modes: {', '.join(prof.failure_modes)}")
            return
        ov = cfg.profile_overrides.setdefault(ref, {})
        scores = ov.setdefault("scores", dict(prof.scores))
        for kv in rest[1:]:
            k, _, v = kv.partition("=")
            if k in CAPABILITIES:
                try:
                    scores[k] = max(0.0, min(1.0, float(v)))
                except ValueError:
                    pass
        save()
        r.profiles = load_profiles(c.ws.config, cfg.profile_overrides)  # live refresh
        return echo(f"[green]updated profile {ref}[/]")
    if sub == "chain":
        if len(rest) >= 2 and rest[0] in CAPABILITIES:
            cfg.chains[rest[0]] = [x for x in
                                   " ".join(rest[1:]).replace(",", " ").split() if x]
            save()
            return echo(f"[green]chain {rest[0]}: {' → '.join(cfg.chains[rest[0]])}[/]")
        return echo(f"[yellow]usage: /router chain <{'|'.join(CAPABILITIES)}> ref,ref,…[/]")
    if sub == "test":
        need = rest[0] if rest and rest[0] in CAPABILITIES else "rtl_author"
        echo(f"[bold]primary pick[/] for need={need}: "
             f"{r.pick(need) or '[dim](pool empty)[/]'}")
        for cap in CAPABILITIES:
            echo(f"  [dim]{cap}:[/] {' → '.join(r.chain(cap)) or '[dim](none)[/]'}")
        return
    if sub == "stats":
        agg = telemetry_stats(c.ws.dot)
        if not agg:
            return echo("[dim]no telemetry yet — enable: /router layer telemetry on[/]")
        for _, a in sorted(agg.items()):
            echo(f"  [bold]{a['model']}[/] / {a['task_class']}: "
                 f"{a['completed']}/{a['runs']} done"
                 f"{', ' + str(a['refused']) + ' refused' if a['refused'] else ''}"
                 f"{', ' + str(a['timed_out']) + ' timeout' if a['timed_out'] else ''}")
        return
    echo("[yellow]/router [status|on|off|layer|triggers|pool|profile|chain|test|stats][/]")



@cli.command("router", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1)
@click.pass_context
def router_cli(ctx, args):
    """Configure auto model-routing (mirrors the `/router` session command).

    Examples: `chipchamp router on`, `chipchamp router pool add
    lmstudio:qwen/qwen3.6-35b-a3b`, `chipchamp router chain debugger
    lmstudio:qwen/qwen3.6-35b-a3b`, `chipchamp router test`, `chipchamp router
    status`. Settings persist to .chipchamp/router.json and apply to
    interactive and headless (`-p`) runs alike."""
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    _router_cmd(c, list(args))


@cli.command(epilog=_tune_epilog())
@click.option("--list", "list_only", is_flag=True, help="list available models and exit")
@click.option("--set", "set_ref", default=None, help="set selection as provider:model")
@click.option("--tune", "tune", multiple=True, metavar="KEY=VALUE",
              help="set a generation parameter for the selected model "
                   "(e.g. --tune num_ctx=8192 --tune temperature=0.7); "
                   "KEY= (empty) clears one. Repeatable.")
@click.option("--tune-global", is_flag=True, help="apply --tune to every model (*)")
@click.option("--tune-reset", is_flag=True, help="clear tuning for the selected model")
@click.option("--show-tune", is_flag=True, help="show effective tuning and exit")
@click.option("--timeout", "timeout", default=None, metavar="SECONDS|Nm|off",
              help="per-model call budget (e.g. --timeout 1200 or 20m or off), "
                   "persisted to .chipchamp/timeout.json")
@click.option("--timeout-global", is_flag=True, help="apply --timeout to every model (*)")
@click.option("--timeout-reset", is_flag=True, help="clear the selected model's budget")
@click.option("--showconfig", is_flag=True, help="show the selected model's settings "
              "as configured in ollama / lm studio, plus chipchamp's overrides")
@click.pass_context
def model(ctx, list_only, set_ref, tune, tune_global, tune_reset, show_tune,
          timeout, timeout_global, timeout_reset, showconfig):
    """Query accessible models across providers and select one (SPEC §14).

    Discovers Anthropic, OpenAI and local OpenAI-compatible servers (LM Studio,
    Ollama, vLLM, llama.cpp), lists their models, and saves your pick to
    .chipchamp/model.json. `--tune KEY=VALUE` sets generation parameters (sent
    verbatim to the server: temperature, top_p, num_ctx, seed, …), persisted to
    .chipchamp/model_opts.json."""
    from ..agent import ModelRegistry
    c = _ctx(ctx.obj["root"], ctx.obj["target"])
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    if set_ref:
        prov, _, mid = set_ref.partition(":")
        reg.save_selection(prov, mid)
        echo(f"[green]selected[/] {prov}:{mid}")
        return
    if showconfig:
        _show_model_config(c)
        return
    if tune or tune_reset or tune_global or show_tune:
        from ..agent.model_opts import parse_kv, reset, set_opts
        ref = _current_ref(c)
        if tune_reset:
            reset(c.ws.dot, ref, glob=tune_global)
        if tune:
            if not tune_global and not ref:
                echo("[yellow]no model selected[/] — `chipchamp model --set "
                     "<provider:model>` first, or use --tune-global")
                raise SystemExit(1)
            setk, unset = parse_kv(tune)
            set_opts(c.ws.dot, ref, setk, unset, glob=tune_global)
        _model_tune_status(c, ref)
        return
    if timeout is not None or timeout_global or timeout_reset:
        from ..agent.model_timeout import reset as _treset, set_timeout
        ref = _current_ref(c)
        if timeout_reset:
            _treset(c.ws.dot, ref, glob=timeout_global)
        if timeout is not None:
            secs = _parse_duration(timeout)
            if secs is None:
                echo(f"[yellow]not a duration:[/] {timeout} [dim]— give seconds "
                     "(1200), minutes (20m), or off[/]")
                raise SystemExit(1)
            if not timeout_global and not ref:
                echo("[yellow]no model selected[/] — `chipchamp model --set "
                     "<provider:model>` first, or use --timeout-global")
                raise SystemExit(1)
            set_timeout(c.ws.dot, ref, secs, glob=timeout_global)
        _timeout_status(c, ref)
        return
    if list_only:
        _list_models(reg)
        return
    _select_model(c)


def _list_models(reg):
    echo("[dim]probing providers (this can take a moment for local servers)…[/]")
    found = reg.discover()
    if not found:
        import importlib.util
        sdk = ("" if importlib.util.find_spec("anthropic") else
               " The anthropic SDK is not installed — `pip install -e '.[model]'`"
               " — so ANTHROPIC_API_KEY alone cannot enable it.")
        echo("[yellow]No reachable providers.[/] Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
             "or start a local server (Ollama :11434, LM Studio :1234)." + sdk)
        return found
    for prov, models in found:
        loc = " [dim](local)[/]" if prov.config.local else ""
        echo(f"\n[bold]{prov.name}[/]{loc}  [dim]{prov.config.base_url}[/]")
        for m in models[:30]:
            echo(f"  {m.id}")
        if len(models) > 30:
            echo(f"  [dim]… {len(models)-30} more[/]")
    return found


def _select_model(c):
    from ..agent import ModelRegistry
    reg = ModelRegistry(c.ws.config, str(c.ws.dot))
    found = _list_models(reg)
    if not found:
        return
    flat = [(prov, m) for prov, models in found for m in models]
    echo("\n[bold]Select a model[/]:")
    for i, (prov, m) in enumerate(flat, 1):
        echo(f"  [cyan]{i:2}[/]  {prov.name}:{m.id}")
    try:
        choice = input("model number (or blank to cancel) » ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(flat)):
        echo("[dim]cancelled[/]")
        return
    prov, m = flat[int(choice) - 1]
    reg.save_selection(prov.name, m.id)
    echo(f"[green]selected[/] {prov.name}:{m.id} [dim](saved to .chipchamp/model.json)[/]")
