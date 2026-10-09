"""chipchamp CLI — repl commands (split from the one-file cli.py)."""
from __future__ import annotations

import sys

import click

from .. import __version__
from .. import brand as _brand

try:
    from rich.table import Table
except Exception:  # pragma: no cover
    Table = None

from ._core import echo, _console, _ctx, _short, cli
from .model import _build_gateway, _build_router, _effort_cmd, _model_tune_cmd, _offer_timeout_retry, _router_cmd, _select_model, _show_model_config, _timeout_cmd


@cli.command()
@click.option("-p", "--prompt", default=None, help="headless one-shot prompt")
@click.option("--model", default=None, help="override model id")
@click.option("--provider", default=None, help="override provider (anthropic/openai/ollama/lmstudio/…)")
@click.option("--max-steps", default=40)
@click.option("--plan", "plan_mode", is_flag=True, default=None,
              help="plan mode: approve each edit/job before it runs")
@click.pass_context
def agent(ctx, prompt, model, provider, max_steps, plan_mode):
    """Start the interactive session (same as running `chipchamp` with no command)."""
    _interactive(ctx.obj, prompt=prompt, model=model, provider=provider,
                 max_steps=max_steps, plan_mode=plan_mode)


# ---- interactive session (the default surface, like claude/codex) -----------

# subcommands that don't make sense as in-session slash commands
_SLASH_EXCLUDE = {"agent", "mcp-serve"}


_LED_KINDS = ("lint", "sim", "synth")


def _board_leds(c, loop):
    """Dev-board LED strip for the input toolbar: one LED per job kind, lit
    from the workspace's LATEST job of that kind (green pass / red fail /
    yellow running / dark never-ran), plus the live model. Job records are
    TTL-cached — the toolbar re-renders on every keystroke."""
    import html as _html
    import time as _time
    now = _time.time()
    cache = getattr(_board_leds, "_cache", None)
    if cache and cache[0] > now - 3.0:
        latest = cache[1]
    else:
        latest = {}
        try:
            for rec in c.runner.list_jobs():  # ascending → later wins
                latest[rec.kind] = rec.status
        except Exception:
            pass
        _board_leds._cache = (now, latest)
    # Lime-phosphor bar: the toolbar paints black-on-phosphor, so plain
    # text needs no styling at all — the MODEL rides as an inverted pill
    # (phosphor-on-black), and the LED strip is monochrome: filled=pass,
    # hollow=never-ran, half=running, with red reserved for failure.
    segs = []
    try:
        ref = getattr(loop.gateway, "ref", "") or ""
        prov, _, mdl = ref.partition(":")
        mdl = (mdl or getattr(loop.gateway, "model", "")
               or "").rsplit("/", 1)[-1][:28]
        eff = getattr(loop.gateway, "reasoning_effort", "") or ""
        if mdl:
            label = f"{prov or 'model'}:{mdl}" + (f" ↯{eff}" if eff else "")
            segs.append(f'<style bg="#000000" fg="#a8ff00"><b> '
                        f'{_html.escape(label)} </b></style>')
    except Exception:
        pass
    kinds = list(_LED_KINDS) + sorted(
        k for k in latest if k in ("formal", "lec", "efpga-fabulous"))
    short = {"efpga-fabulous": "efpga"}
    cells = []
    for k in kinds:
        st = latest.get(k)
        if st == "passed":
            led = "●"
        elif st in ("failed", "error", "timeout"):
            led = "<ansired>●</ansired>"
        elif st == "running":
            led = "◐"
        else:
            led = "○"
        cells.append(f"{short.get(k, k)} {led}")
    if cells:
        segs.append("  ".join(cells))
    return "  │  ".join(segs)


def _print_failure_waves(c, jid: str) -> None:
    """Render a failing sim's VCD tail inline (logic-analyzer view). Pure
    diagnosis candy layered over the real job record — any failure to render
    must be silent, never a session error."""
    from .. import ui
    try:
        store = c.resolve_wave(jid)
        if store is None:
            return
        from ..waves.render import analyzer_view
        for ln in analyzer_view(store, width=ui.console().size.width):
            ui.console().print(ln)
    except Exception:
        pass


_SPARK = "▁▂▃▄▅▆▇█"


def _burndown(hist: list[int]) -> str:
    """Lint-error history as a sparkline: `8▇ 3▃ 0▁`, red while burning,
    green at zero — converging vs grinding at a glance."""
    mx = max(hist) or 1
    cells = []
    for v in hist[-12:]:
        ch = _SPARK[min(7, round(v / mx * 7))]
        col = "green" if v == 0 else "red"
        cells.append(f"[{col}]{v}{ch}[/]")
    return " ".join(cells)


def _persist_theme(c, name: str) -> None:
    """Write `[ui] theme` into the workspace config so the choice survives
    the session. Surgical: replaces an existing theme line, extends an
    existing [ui] section, or appends one — comments elsewhere untouched."""
    import re as _re
    path = c.ws.dot / "config.toml"
    try:
        text = path.read_text() if path.exists() else ""
    except OSError:
        return
    line = f'theme = "{name}"' if name else 'theme = ""'
    if _re.search(r"(?m)^\s*theme\s*=", text):
        text = _re.sub(r"(?m)^\s*theme\s*=.*$", line, text, count=1)
    elif _re.search(r"(?m)^\[ui\]", text):
        text = _re.sub(r"(?m)^\[ui\]\s*$", f"[ui]\n{line}", text, count=1)
    else:
        text += f"\n[ui]\n{line}\n"
    try:
        path.write_text(text)
    except OSError:
        pass


def _agent_events(c):
    """Render the agent's stream like claude-code: compact tool calls, colored
    diffs for edits, Markdown for prose (fenced SystemVerilog gets highlighted),
    tokens streaming live as the model generates (reasoning scrolls by in the
    ticker), and a rolling tail of the live log while an EDA job runs."""
    from .. import ui
    state = {"think": None, "job": None, "streamed": 0, "pulse": None}

    # the runner announces each step's live log; hand it to the active tail
    def _on_step(_job_id, live_path):
        tail = state.get("job")
        if tail is not None:
            tail.attach(live_path)
    c.runner.on_step = _on_step

    def _show_diff(path):
        e = c.edits.get(path)
        if e:
            ui.diff(e["old"], e["new"], path)

    def _show_ladder():
        """Repaint the gate ladder — but only when it actually moved.

        A ladder redrawn identically after every job is noise, and noise is how
        a HUD stops being read. It also stays quiet until there is something to
        judge: on a pure investigation nothing has been edited, so every gate
        would read `missing` and the line would be a row of dots implying the
        agent is failing at work nobody asked for."""
        if not c.edits:
            return
        rep = c.gate_status()
        fp = ui.gate_fingerprint(rep)
        if not fp or fp == state.get("ladder"):
            return
        state["ladder"] = fp
        ui.console().print("  " + ui.gate_ladder(rep))

    def on_event(kind, data):
        sub = data.get("subagent")
        if sub:
            # Subagent traffic gets its own compact, LABELED path. Unlabeled,
            # a role-denied tool call renders exactly like a parent failure —
            # in the blind eFPGA triage run a burst of least-privilege
            # denials read as a platform-wide tool lockout to the human
            # watching. And a subagent's thinking_start must never spawn a
            # StreamView: it would fight the parent's live spinner.
            tag = f"  [dim]{sub} ▸[/]"
            if kind == "tool_call":
                ui.console().print(
                    f"{tag} [dim]→ {data['name']}"
                    f" {_action_desc(data['name'], data.get('input', {}))}[/]")
            elif kind == "tool_result":
                res = data.get("result", {})
                if isinstance(res, dict):
                    if res.get("denied"):
                        ui.console().print(f"{tag} [yellow]⊘ "
                                           f"{data.get('name', '')}[/]")
                    elif "error" in res:
                        ui.console().print(
                            f"{tag} [red]✗ {data.get('name', '')}: "
                            f"{str(res['error'])[:80]}[/]")
            elif kind == "job_done":
                res = data.get("result", {})
                if isinstance(res, dict) and res.get("job"):
                    ui.console().print(
                        f"{tag} [dim]▪ {res['job']} {data.get('name', '')} "
                        f"{res.get('status', 'done')}[/]")
            elif kind == "model_switch":
                ui.console().print(f"{tag} [dim]{data['from']} → "
                                   f"{data['to']} ({data.get('reason', '')})[/]")
            elif kind == "error":
                ui.console().print(f"{tag} [red]{data.get('message', '')}[/]")
            return
        if kind == "thinking_start":
            state["streamed"] = 0
            state["think"] = ui.StreamView(data.get("model", ""),
                                           pulse=state["pulse"])
            state["think"].start()
        elif kind == "delta":
            if state["think"]:
                state["think"].feed(data.get("channel", ""),
                                    data.get("chunk", ""))
        elif kind == "thinking_done":
            if state["pulse"] is not None:
                # exact usage for the call that just finished; the spinner only
                # estimates the one still streaming
                state["pulse"].add_tokens(data.get("output_tokens", 0))
            if state["think"]:
                state["think"].stop()
                # remember whether this turn's text already rendered live, so
                # the upcoming `assistant` event doesn't print it twice
                state["streamed"] = state["think"].streamed_chars
                state["think"] = None
        elif kind == "job_start":
            # a real EDA job is about to block — tail its live log while it runs
            desc = _action_desc(data["name"], data.get("input", {}))
            state["job"] = ui.JobTail(f"{data['name']} {desc}",
                                      pulse=state["pulse"])
            state["job"].start()
        elif kind == "job_done":
            if state["job"]:
                state["job"].stop()
                state["job"] = None
            res = data.get("result", {})
            if isinstance(res, dict):
                jid = res.get("job", "")
                status = res.get("status") or res.get("sim_status") \
                    or res.get("verdict") or ("error" if "error" in res else "done")
                col = "green" if status in ("passed", "pass", "done") else \
                    ("red" if status in ("failed", "error", "fail") else "yellow")
                if jid:
                    ui.console().print(
                        f"  [dim]▪[/] [bold]{jid}[/] {data['name']} "
                        f"[{col}]{status}[/]"
                        + (f" [dim]· {str(res['summary'])[:70]}[/]"
                           if res.get("summary") else ""))
                # post-mortem at a glance: a failing sim that dumped waves
                # gets its last cycles rendered inline, analyzer-style
                if (data["name"] == "sim.run" and res.get("waves_job")
                        and str(status) in ("failed", "fail", "error")):
                    _print_failure_waves(c, res["waves_job"])
            _show_ladder()
        elif kind == "budget_block":
            ui.warn(f"  ⚠ budget: {data.get('reason', '')}")
        elif kind == "compacted":
            # say it out loud: a body left the context, and the human should
            # know the model is now working from digests plus job ids
            ui.console().print(
                f"  [dim]▪ context compacted — {data['evicted_results']} old "
                f"result(s) evicted to their job ids "
                f"({data['before'] // 1000}k → {data['after'] // 1000}k chars)[/]")
        elif kind == "tools_loaded":
            ui.console().print(
                f"  [dim]▪ loaded {len(data['added'])} tool(s) from "
                f"{', '.join(data['groups'])}[/]")
        elif kind == "destructive":
            ui.warn(f"  ⚠ destructive: {data['name']} — will ask to confirm")
        elif kind == "tool_call":
            if not data["name"].startswith("plan."):  # plan renders on its result
                ui.tool_call(data["name"], data["input"])
        elif kind == "tool_result":
            name, res = data.get("name", ""), data.get("result", {})
            if not isinstance(res, dict):
                return
            if name.startswith("plan."):
                ui.plan(res.get("plan", []))
            elif res.get("denied"):
                ui.console().print(f"  [yellow]⊘ skipped {name} (not approved)[/]")
            elif "error" in res:
                ui.console().print(f"  [red]✗ {name}: {str(res['error'])[:100]}[/]")
            elif isinstance(res.get("waveform"), dict) and res["waveform"].get("format") == "text":
                ui.waveform(res.get("text", ""), path=res["waveform"]["path"])
                if res.get("images"):
                    ui.show_images(res["images"], mode=_image_mode(c), relto=c.ws.root)
            elif name == "wave.snapshot" and res.get("ascii"):
                ui.waveform(res["ascii"])
            elif res.get("images"):
                # an MCP renderer (e.g. wavelets' render_waveform_png) handed
                # back a picture; the base64 never entered the transcript, so
                # this is the only place it becomes visible
                ui.show_images(res["images"], mode=_image_mode(c), relto=c.ws.root)
            elif name in ("fs.edit", "fs.write") and res.get("path"):
                _show_diff(res["path"])
                # the first edit is what CREATES the obligation — it decides the
                # task class, and therefore which gates are owed. Showing the
                # ladder here means the bill arrives with the change, not at
                # report.done when it is too late to plan around.
                _show_ladder()
            elif name == "lint.run" and isinstance(res.get("errors"), int):
                hist = state.setdefault("lint_hist", [])
                hist.append(res["errors"])
                n = res["errors"]
                col = "green" if n == 0 else "red"
                line = (f"  [dim]▪[/] [bold]{res.get('job', 'lint')}[/] "
                        f"lint [{col}]{n} error(s)[/]")
                if len(hist) >= 2:  # one point isn't a burndown yet
                    line += f"  [dim]⌁[/] {_burndown(hist)}"
                ui.console().print(line)
            elif name == "regmap.generate" and res.get("ok"):
                for w in res.get("written", []):
                    if w.get("changed"):
                        _show_diff(w["path"])
        elif kind == "model_switch":
            ui.handoff(data["from"], data["to"], data.get("reason", ""))
        elif kind == "assistant" and data.get("text"):
            # already on screen if it streamed live; render only otherwise
            # (off-TTY, non-streaming provider, or a zero-content turn)
            if not state.pop("streamed", 0):
                ui.markdown(data["text"])
            state["streamed"] = 0
        elif kind == "error":
            ui.error(data["message"])
        elif kind == "security":
            kinds = ", ".join(f["kind"] for f in data["findings"][:3])
            ui.warn(f"  ⚠ injection-shaped content in {data['tool']} output "
                    f"({kinds}) — treated as data, policy unaffected")

    def shutdown():
        """Stop any live renderer (ctrl-c mid-generation would otherwise leave
        a daemon thread repainting over the prompt forever)."""
        for key in ("think", "job"):
            if state.get(key):
                state[key].stop()
                state[key] = None

    def begin_task(text: str, effort: str = ""):
        """Open the task-level clock the step spinners report against."""
        state["pulse"] = ui.TaskPulse(text, effort)
        return state["pulse"]

    def end_task():
        state["pulse"] = None

    on_event.shutdown = shutdown
    on_event.begin_task = begin_task
    on_event.end_task = end_task
    return on_event


def _session_commands(c=None) -> dict:
    """name -> menu entry, for the slash completion menu. Built-in commands map
    to a plain help string; discovered skills map to (help, "skill") so the menu
    can set them apart (ui.menu_entry) instead of rendering both identically.

    Built-ins keep their name on a collision, and the shadowed skill is offered
    as ``/skill:<name>`` — it used to just vanish from the menu."""
    cmds = {n: (c_.get_short_help_str(60) or "")
            for n, c_ in cli.commands.items() if n not in _SLASH_EXCLUDE}
    cmds.update({"help": "show commands", "model": "switch provider/model",
                 "effort": "reasoning effort: low/medium/high · off pins · auto unpins",
                 "timeout": "per-model call budget (raise it for a slow model)",
                 "router": "auto model-routing: on/off, pool, profiles, chains",
                 "mode": "cycle autonomy: normal/auto/plan (shift-tab)",
                 "theme": "UI theme: phosphor · amber · scope · mono · off",
                 "plan": "toggle plan mode (approve edits/jobs)",
                 "sessions": "list / resume saved sessions",
                 "undo": "revert the files the last task changed",
                 "clear": "reset the conversation", "exit": "quit the session"})
    if c is not None:
        try:
            from ..skills import discover, first_sentence
            sk = discover(c.ws)
            if sk:
                cmds.setdefault("skill", "load a skill playbook into the conversation")
                for s in sk.values():
                    key = s.name if s.name not in cmds else f"skill:{s.name}"
                    cmds.setdefault(key,
                                    (first_sentence(s.description)[:60], "skill"))
        except Exception:
            pass  # a broken skills dir must not break the REPL
    return dict(sorted(cmds.items()))


_GATE_PERMS = {"write", "submit", "approve"}   # approve: waiver drafts
_PLAN_SUFFIX = (
    "PLAN MODE is ON: file writes/edits, job-launching tools and waiver drafts require the "
    "human's approval before they run. Keep your plan.update checklist current "
    "and expect to be gated on each mutating action; if an action is declined, "
    "revise the plan rather than retrying it.")


def _apply_plan_mode(loop, on: bool) -> None:
    loop.gate_permissions = set(_GATE_PERMS) if on else set()
    loop.system_suffix = _PLAN_SUFFIX if on else ""


# ---- autonomy modes (item 3): cycled with shift-tab or /mode ----------------
# Destructive actions are ALWAYS confirmed regardless of mode (item 8).
_MODE_ORDER = ["normal", "auto", "plan"]
_MODE_DESC = {
    "normal": "edits & jobs run; destructive actions confirmed",
    "auto":   "full autonomy; only destructive actions confirmed",
    "plan":   "approve every edit & job before it runs",
}
# one color per mode, everywhere a mode name renders (echoes + ptk toolbar):
# green = safest (everything approved), cyan = house accent, yellow = caution
_MODE_COLOR = {"normal": "cyan", "auto": "yellow", "plan": "green"}


class _AutonomyMode:
    """Session autonomy state shared by the input controller (shift-tab) and
    the /mode command; applies itself to the loop's gating on every change."""

    def __init__(self, loop, name: str = "normal"):
        self.loop = loop
        self.name = name if name in _MODE_ORDER else "normal"
        self.apply()

    @property
    def color(self) -> str:
        """Per-mode accent color; ui.InputController colors the toolbar with it."""
        return _MODE_COLOR[self.name]

    def apply(self) -> None:
        _apply_plan_mode(self.loop, self.name == "plan")

    def cycle(self) -> str:
        self.name = _MODE_ORDER[(_MODE_ORDER.index(self.name) + 1) % len(_MODE_ORDER)]
        self.apply()
        return self.name

    def set(self, name: str) -> bool:
        if name not in _MODE_ORDER:
            return False
        self.name = name
        self.apply()
        return True


def _action_desc(name, args) -> str:
    a = args or {}
    for key in ("path", "test", "tag", "top", "module", "spec", "branch"):
        if key in a:
            return f"{key}={a[key]}"
    return _short(a)


def _make_approver():
    """Interactive approval. Two paths:
      • destructive ops (perm='destructive') — ALWAYS confirmed individually,
        with a stronger prompt, never covered by an earlier 'approve all', in
        every autonomy mode (item 8);
      • plan-mode gating of writes/jobs — y / a(ll) / N.
    Off a TTY both decline (safe: a headless run can't delete or auto-apply)."""
    from .. import ui
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    allow = {"all": False}

    def approve(name, args, perm):
        destructive = perm == "destructive"
        if allow["all"] and not destructive:
            return True  # 'approve all' never covers destructive actions
        if destructive:
            ui.warn(f"  ⚠ DESTRUCTIVE: {name}  {_action_desc(name, args)}")
            if not tty:
                ui.warn("     non-interactive → declining (destructive actions "
                        "need an interactive confirm).")
                return False
            try:
                ans = input("     permanently proceed? type 'yes' to confirm: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            ok = ans == "yes"
            ui.info("     [green]confirmed[/]" if ok else "     [dim]cancelled[/]")
            return ok
        ui.approval_request(name, _action_desc(name, args))
        if not tty:
            ui.warn("  non-interactive + plan mode → declining (preview only); "
                    "switch to normal/auto mode (shift-tab or /mode) to apply.")
            return False
        try:
            ans = input("     approve? [y]es once · [a]ll · [N]o : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans == "a":
            allow["all"] = True
            ui.info("     [dim]approving all further (non-destructive) actions[/]")
            return True
        ok = ans in ("y", "yes")
        ui.info("     [green]approved[/]" if ok else "     [dim]skipped[/]")
        return ok

    return approve


def _undo_cmd(c) -> None:
    """`/undo` — put the tree back the way it was before the last task."""
    out = c.undo()
    if out.get("error"):
        echo(f"[yellow]{out['error']}[/] [dim]— nothing has been edited yet "
             f"this session[/]")
        return
    for p in out["restored"]:
        echo(f"  [green]reverted[/] {p}")
    for p in out["deleted"]:
        echo(f"  [green]removed[/]  {p} [dim](created by that task)[/]")
    for p in out["skipped"]:
        echo(f"  [yellow]kept[/]     {p} [dim](changed on disk since — your "
             f"edit, not the agent's)[/]")
    if not (out["restored"] or out["deleted"] or out["skipped"]):
        echo("[dim]nothing to undo — that task changed no files[/]")
    else:
        echo(f"[dim]undone: {out.get('checkpoint') or 'last task'}[/]")


def _image_mode(c) -> str:
    """`[ui] mcp_images` — how a picture from an MCP tool is presented.

    auto (default) draws it when the terminal has an inline protocol and links
    it otherwise; `image` is the same but says why it could not draw; `off`
    always links. Text chronograms are a different TOOL, not a fallback, so
    `off` is a preference for the rendering that survives asciinema, SSH and
    tmux rather than a degraded mode."""
    mode = str((c.ws.config.get("ui", {}) or {}).get("mcp_images", "auto")).lower()
    return mode if mode in ("auto", "image", "off") else "auto"


def _context_budget(c, gw) -> int:
    """Transcript budget for this session, or 0 to never compact.

    Off for a NullGateway (there is no run to protect) and honours an explicit
    `[model] context_chars = 0` for anyone who would rather hit a hard wall
    than have a tool result evicted behind their back."""
    if gw is None:
        return 0
    from ..agent.working_set import budget_for
    return budget_for(gw, c.ws.config)


def _make_budget_approver(c):
    """FR-BUDG-01: a ceiling pauses and ASKS. Interactive sessions can approve
    an overrun; a headless run cannot, and stops — which is the whole point of
    running one unattended."""
    from .. import ui
    from ..policy.budgets import config_hint

    def approve(kind: str, why: str) -> bool:
        ui.warn(f"  [bold yellow]budget[/] {why}")
        if not sys.stdin.isatty():
            ui.warn("  non-interactive → stopping at the ceiling (raise "
                    f"[bold]{config_hint(kind)}[/] in .chipchamp/config.toml "
                    "to go on)")
            return False
        try:
            ans = input("     raise it for this task? type 'yes': ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        ok = ans == "yes"
        if ok:
            # one task's reprieve, not a new ceiling: the budget in config is
            # still what tomorrow's run gets
            b = c.ledger.budget
            if kind == "license_hours":
                b.license_hours += 4.0
            elif kind == "v3_submissions":
                b.v3_submissions += 1
        ui.info("     [green]raised for this task[/]" if ok
                else "     [dim]stopped at the ceiling[/]")
        return ok

    return approve


def _session_user_inputs(sess) -> list[str]:
    """Genuine user requests from a session, oldest→newest, excluding the loop's
    synthetic nudge turns. New sessions flag real inputs (`input: True`); for
    older sessions predating the flag, fall back to every text user turn."""
    msgs = [m for m in sess.messages if isinstance(m, dict)]
    marked = any("input" in m for m in msgs)
    out = []
    for m in msgs:
        if m.get("role") != "user" or not isinstance(m.get("content"), str):
            continue
        if marked and not m.get("input"):
            continue  # a synthetic nudge, not the user
        s = m["content"].strip()
        if s:
            out.append(s)
    return out


def _replay_session(sess, max_turns: int = 40) -> None:
    """Print a resumed session's prior conversation so the context is visible,
    not just referenced. Renders genuine user turns and assistant replies;
    tool-result turns and synthetic nudges are skipped."""
    from rich.markup import escape

    from .. import ui
    msgs = [m for m in sess.messages if isinstance(m, dict)]
    marked = any("input" in m for m in msgs)
    turns = []
    for m in msgs:
        role, content = m.get("role"), m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            if marked and not m.get("input"):
                continue
            turns.append(("user", content.strip()))
        elif role == "assistant":
            turns.append(("assistant", content.strip()))
    if not turns:
        return
    shown = turns[-max_turns:]
    echo("[dim]──────── previous conversation ────────[/]")
    if len(turns) > len(shown):
        echo(f"[dim]  (… {len(turns) - len(shown)} earlier turns omitted)[/]")
    for role, content in shown:
        if role == "user":
            echo(f"[cyan]»[/] {escape(content)}")
        else:
            ui.markdown(content)
    echo("[dim]───────────────────────────────────────[/]")


def _interactive(ctx_obj, prompt=None, model=None, provider=None, max_steps=40,
                 plan_mode=None, resume=None, experiment=None):
    from .. import ui
    from ..agent import AgentLoop, NullGateway, Session
    from ..mcp import load_mcp_tools
    from ..tools import all_tools as _all
    c = _ctx(ctx_obj["root"], ctx_obj["target"])
    c.ws.ensure_work_dir()  # the agent's scratch/design dir (item: work dir)
    for w in getattr(c.ws, "config_warnings", []):
        ui.warn(f"  [yellow]config[/] {w}")
    gw, reason, audit = _build_gateway(c, model, provider)
    has_model = gw is not None
    if not has_model:
        gw = NullGateway()

    # plan mode default: explicit --plan, else on when the policy's default
    # autonomy is L0 ("suggest") — the human applies everything.
    if plan_mode is None:
        plan_mode = c.policy.default_autonomy == "L0"

    mcp_tools, mcp_clients = load_mcp_tools(
        c.ws.config, cwd=c.ws.root,
        image_dir=str(c.ws.dot / "mcp-images"),
        on_warning=lambda m: ui.warn(f"  [yellow]mcp[/] {m}"))
    tool_set = {**_all(), **mcp_tools} if mcp_tools else None
    store = str(c.ws.dot / "sessions")
    sess, resumed = None, None
    if resume == "__latest__":
        sess = Session.latest(store)
        resumed = "most recent" if sess else None
    elif resume:
        sess = Session.load(resume, store)
        resumed = resume if sess else None
        if sess is None:
            echo(f"[yellow]no session '{resume}' in this workspace[/] — "
                 f"starting fresh (`/sessions` lists them)")
    if sess is None:
        sess = Session.new(store, target=c.target_name)
    events = _agent_events(c)
    # Progressive tool disclosure: on by default for local servers, where the
    # context window is the binding constraint (the full catalog is ~8.4k
    # tokens of schema on every request; the core set is ~2.1k). Frontier
    # models keep the whole catalog unless the workspace says otherwise.
    disclose = c.ws.config.get("model", {}).get(
        "tool_disclosure",
        str(getattr(gw, "ref", "")).startswith(("ollama:", "lmstudio:")))
    c.budget_approver = _make_budget_approver(c)
    loop = AgentLoop(c, gw, session=sess, max_steps=max_steps,
                     on_event=events, tools=tool_set,
                     approver=_make_approver(), router=_build_router(c),
                     disclose=disclose, context_budget=_context_budget(c, gw),
                     **c.ws.guards(headless=bool(prompt)).as_kwargs())
    mode = _AutonomyMode(loop, "plan" if plan_mode else "normal")

    # headless one-shot (`chipchamp -p "..."`)
    if prompt:
        if not has_model:
            echo(f"[yellow]No model configured:[/] {reason}")
            raise SystemExit(2)
        rec = None
        if experiment:
            from ..bench.experiment import ExperimentRecorder
            rec = ExperimentRecorder(c.ws.dot, label=experiment, task=prompt)
            rec.attach(loop)
        out = loop.run(prompt)
        sess.save()
        if rec is not None:
            exp = rec.finish(out, max_steps=max_steps)
            echo(f"[dim]— experiment [bold]{exp.id}[/] ({exp.outcome}) "
                 f"recorded under {c.rel(rec.dir)}[/]")
        # final text already rendered by the event stream (assistant events,
        # incl. synthesized stop notes) — re-echoing printed every answer twice
        extras = "".join(
            f", {out[k]} {lbl}" for k, lbl in
            (("recovered_calls", "recovered-calls"),
             ("empty_turns", "empty-turns"),
             ("length_stops", "length-stops")) if out.get(k))
        echo(f"[dim]— {out['steps']} steps, {out.get('tokens', 0)} tokens{extras}, "
             f"model {out.get('model', '?')}, "
             f"jobs: {', '.join(out.get('jobs', [])) or 'none'} — audit: {c.rel(audit)}[/]")
        return

    ui.clear()  # open as its own app (TTY only)
    _print_banner(c, gw, has_model, reason, len(mcp_tools) if mcp_tools else 0,
                  ctx_obj.get("root_how", ""), plan_on=bool(loop.gate_permissions),
                  mode=mode)
    if resumed:
        echo(f"  [green]resumed[/] {resumed} session [bold]{sess.id}[/] "
             f"[dim]({len(sess.messages)} messages) — /sessions to switch[/]")
        _replay_session(sess)
    inp = ui.InputController(
        _session_commands(c), mode=mode,
        on_mode_change=lambda name: echo(
            f"  [dim]autonomy → [/][bold {_MODE_COLOR[name]}]{name}[/] "
            f"[dim]{_MODE_DESC[name]}[/]"),
        toolbar_extra=lambda: _board_leds(c, loop))
    if resumed:  # make the resumed session's requests recallable with up/down
        inp.remember(_session_user_inputs(sess))
    pending: list[str] = []  # /skill playbooks staged for the next task
    while True:
        inp.separator()
        try:
            line = inp.read()
        except (EOFError, KeyboardInterrupt):
            echo("")
            break
        if not line:
            continue
        if line.startswith("!"):
            # Shell passthrough: run it exactly as a terminal would — stdio
            # inherited (interactive commands work), cwd = workspace root —
            # and leave NO trace in the session: not in the transcript, not
            # in the model's context, not in the audit. The user talking to
            # their own shell is not part of the agent's conversation.
            cmd = line[1:].strip()
            if cmd:
                import subprocess
                try:
                    subprocess.run(cmd, shell=True, cwd=c.ws.root)
                except KeyboardInterrupt:
                    echo("")
            continue
        if line.startswith("/"):
            if _dispatch_slash(ctx_obj, c, loop, line,
                               pending if has_model else None, mode=mode,
                               inp=inp) == "exit":
                break
            continue
        if line in ("exit", "quit"):
            break
        if not has_model:
            ui.warn("No model configured, so plain-English tasks can't run. "
                    "Use /model to pick one, or slash-commands like /sim, /lint, "
                    "/triage (they work without a model).")
            continue
        # the status line names the task in the user's own words, so it is
        # built BEFORE any staged skill playbook is prepended to the prompt
        events.begin_task(line, getattr(loop.gateway, "reasoning_effort", ""))
        # mark the tree so `/undo` can put this one task back, and only this
        # one — an undo that unwound three tasks at once would be unusable
        c.checkpoint(ui.task_label(line, 60))
        if pending:
            line = "\n\n".join(pending) + f"\n\n---\nTask: {line}"
            pending.clear()
        try:
            out = loop.run(line)
            # a too-slow model: offer a bigger-budget retry instead of just
            # losing the turn (loop only flags this once the router can't swap).
            while out.get("timed_out") and \
                    _offer_timeout_retry(c, loop, line, out) == "retry":
                out = loop.run(line)
        except KeyboardInterrupt:
            events.shutdown()  # stop live renderers before touching the screen
            echo("[dim](interrupted)[/]")
        finally:
            events.end_task()
        sess.save()
    for mc in (mcp_clients or []):
        mc.close()


def _git_rev(root: str) -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# The package is FILLED, so the die reads as a solid part rather than an
# outline. grey27 is the only fill that survives both extremes: dark enough to
# look like epoxy (9.7:1 against a white terminal) and light enough to stay
# visible on a black one (1.7:1) — grey15 vanishes on a dark theme (1.01:1
# against solarized-dark). The silkscreen is lifted to match: bright_black
# only manages 2.5:1 on the fill, grey70 makes 4.1:1 and cyan 7.8:1.
_CHIP_FILL = "grey27"
_CHIP_LEAD = "grey37"                    # leads, sit on the terminal background
# silkscreen for a dark package / for a light one: (outline, stamp, name)
_CHIP_INK = {False: ("grey62", "grey70", "bold cyan"),
             True: ("grey11", "grey23", "bold #005f87")}


def _chip_ink(fill: str) -> tuple[str, str, str, str]:
    """(fill, outline, stamp, name) for a package fill. The ink follows the
    fill's luminance — light on a dark package, dark on a light one — so a
    configured colour stays legible instead of disappearing into its own body.
    An unparseable fill falls back: the banner must never fail to render."""
    try:
        from rich.color import Color as _RColor
        r, g, b = _RColor.parse(fill).get_truecolor()
    except Exception:
        fill, (r, g, b) = _CHIP_FILL, (68, 68, 68)
    lin = [c / 255 for c in (r, g, b)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in lin]
    light = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2] > 0.18
    return (fill, *_CHIP_INK[light])


def _chip_banner(proj: str, root: str, fill: str = "") -> list[str]:
    """The session opens on a die shot: the workspace is the chip, silkscreened
    with its name and stamped lot-code style (date + git rev) — the tool reads
    as a piece of hardware from the first frame.

    The package is filled (``[ui] chip_fill`` overrides the default epoxy
    grey), so it reads as a solid part rather than an outline."""
    import time as _time
    name = (proj or _brand.APP_NAME)[:13]
    yw = _time.strftime("%y%W")  # year+week, like real silicon lot codes
    rev = _git_rev(root)[:7]
    stamp = (f"{yw} · {rev}" if rev else f"lot {yw}")[:15]
    inner = 15
    fill, outline, ink, silk = _chip_ink(fill or _CHIP_FILL)
    edge = f"{outline} on {fill}"

    def row(text: str, style: str) -> str:
        """One package row: lead, wall, silkscreen, wall, lead. The fill runs
        wall to wall — centring inside the styled span keeps the padding filled
        too, so the body stays a solid rectangle."""
        return (f"    [{_CHIP_LEAD}]─[/][{edge}]┤[/]"
                f"[{style} on {fill}]{text:^{inner}}[/]"
                f"[{edge}]├[/][{_CHIP_LEAD}]─[/]")

    return [
        f"     [{edge}]┌{'┬' * inner}┐[/]",
        row(f"{_brand.APP_NAME} {__version__}", ink),
        row(name, silk),
        row(stamp, ink),
        f"     [{edge}]└{'┴' * inner}┘[/]",
    ]


def _print_banner(c, gw, has_model, reason, n_mcp, how="", plan_on=False,
                  mode=None):
    """Die shot on the left, session info silkscreened beside it — one compact
    block instead of the chip floating above a paragraph."""
    from rich.text import Text
    proj = c.ws.config.get("project", {}).get("name") or c.ws.default_target
    nmods = len(c.ws.db().modules)
    chip = _chip_banner(proj, c.ws.root,
                        fill=c.ws.config.get("ui", {}).get("chip_fill", ""))
    info = ["[dim]agentic RTL design & verification[/]",
            f"[dim]repo[/] {c.ws.root}" + (f" [dim]({how})[/]" if how else "")]
    line = f"[dim]target[/] {c.target_name} [dim]({nmods} modules)[/]"
    if has_model:
        line += f"  [dim]·  model[/] {gw.ref}"
    if n_mcp:
        line += f"  [dim]·  mcp[/] {n_mcp} tool(s)"
    if plan_on:
        line += "  [dim]·[/] [yellow]plan mode[/]"
    info.append(line)
    if has_model:
        hint = "[dim]Type a task in plain English.  /help for commands · /exit to quit[/]"
        if plan_on:
            hint = ("[dim]Plan mode: I'll show a plan and ask before edits/jobs. "
                    "/plan off to disable · /help · /exit[/]")
        info.append(hint)
    else:
        info.append(f"[yellow]no model:[/] [dim]{reason}[/]")
        info.append("[dim]/model to pick one · slash-commands (/sim /lint /triage) "
                    "work without a model · /help[/]")
    if mode is not None:
        info.append(f"[dim]autonomy:[/] [bold {mode.color}]{mode.name}[/] "
                    f"[dim]— {_MODE_DESC[mode.name]} · shift-tab or /mode[/]")
    echo("")
    col = max(Text.from_markup(ln).cell_len for ln in chip) + 2
    for i in range(max(len(chip), len(info))):
        left = chip[i] if i < len(chip) else ""
        right = info[i] if i < len(info) else ""
        pad = " " * (col - (Text.from_markup(left).cell_len if left else 0))
        # The two halves are printed as one Text so they can be highlighted
        # differently: the chip is a hand-styled graphic and the repr
        # highlighter must not recolor its version digits, while the info
        # beside it keeps the highlighting it has always had.
        out = Text.from_markup(left)
        if right:
            out.append(pad)
            out.append_text(_console().render_str(right, highlight=True) if _console()
                            else Text.from_markup(right))
        echo(out)
    echo("")


def _dispatch_slash(ctx_obj, c, loop, line, pending=None, mode=None,
                    inp=None) -> str | None:
    import shlex
    try:
        parts = shlex.split(line[1:])
    except ValueError:
        parts = line[1:].split()
    if not parts:
        return None
    name, args = parts[0], parts[1:]
    if name in ("exit", "quit", "q"):
        return "exit"
    if name == "skill":
        if not args or args[0] == "list":
            _print_skills(c)
        else:
            _load_skill(c, loop, args[0], pending)
        return None
    if name.startswith("skill:"):
        # qualified form for a skill shadowed by a built-in of the same name
        _load_skill(c, loop, name[len("skill:"):], pending)
        return None
    if name == "sessions":
        _sessions_cmd(c, loop, args, inp=inp)
        return None
    if name in ("help", "?", "h"):
        _repl_help(c)
        return None
    if name == "clear":
        loop.session.messages = []
        loop.session.save()
        echo("[dim]conversation cleared[/]")
        return None
    if name == "undo":
        _undo_cmd(c)
        return None
    if name == "theme":
        from .. import ui as _ui
        if not args:
            cur = _ui._active_theme or "default"
            echo(f"theme: [bold]{cur}[/] — available: "
                 + ", ".join(_ui.theme_names() + ["off"]))
            return None
        want = args[0].lower()
        applied = _ui.set_theme(want)
        if want not in ("off", "default", "none") and applied != want:
            echo(f"[yellow]unknown theme '{want}'[/] — available: "
                 + ", ".join(_ui.theme_names() + ["off"]))
            return None
        _persist_theme(c, applied)
        echo(f"theme: [bold]{applied or 'default'}[/] [dim](applied live, "
             f"saved to .chipchamp/config.toml)[/]")
        return None
    if name == "mode":
        if mode is None:
            echo("[yellow]autonomy modes aren't available here[/]")
            return None
        if args and args[0].lower() in _MODE_ORDER:
            mode.set(args[0].lower())
        else:
            mode.cycle()
        echo(f"[dim]autonomy → [/][bold {mode.color}]{mode.name}[/] "
             f"[dim]{_MODE_DESC[mode.name]}[/]")
        return None
    if name == "plan":
        # kept for muscle memory; plan is one of the three autonomy modes now
        on = not (mode.name == "plan" if mode else bool(loop.gate_permissions))
        if args and args[0].lower() in ("on", "off"):
            on = args[0].lower() == "on"
        if mode is not None:
            mode.set("plan" if on else "normal")
        else:
            _apply_plan_mode(loop, on)
        echo(f"[green]plan mode {'on' if on else 'off'}[/]"
             + (f" [dim]— autonomy:[/] [{mode.color}]{mode.name}[/]" if mode else ""))
        return None
    if name == "model":
        if args and args[0] in ("tune", "opts", "params"):
            _model_tune_cmd(c, list(args[1:]), loop=loop)
            return None
        if args and args[0] in ("showconfig", "config", "show"):
            _show_model_config(c)
            return None
        _select_model(c)
        gw2, _, _ = _build_gateway(c)
        if gw2:
            loop.gateway = gw2
            echo(f"[green]switched to {gw2.ref}[/]")
        return None
    if name == "router":
        _router_cmd(c, args, loop=loop)
        return None
    if name == "effort":
        _effort_cmd(c, args, loop=loop)
        return None
    if name == "timeout":
        _timeout_cmd(c, args, loop=loop)
        return None
    if name in _SLASH_EXCLUDE:
        echo(f"[yellow]/{name} is not available in the session[/]")
        return None
    cmd = cli.get_command(None, name)
    if cmd is None:
        # not a built-in: a discovered skill name dispatches as /skill <name>
        # (built-ins always win; /skill <name> is the shadowed-skill escape)
        try:
            from ..skills import discover
            if name in discover(c.ws):
                _load_skill(c, loop, name, pending)
                return None
        except Exception:
            pass
        echo(f"[yellow]unknown command /{name}[/] — try [bold]/help[/] "
             f"or [bold]/skill list[/]")
        return None
    try:
        cmd.main(args=args, prog_name=f"/{name}", standalone_mode=False, obj=ctx_obj)
    except SystemExit:
        pass  # e.g. /ci exits, or /<cmd> --help
    except click.ClickException as e:
        e.show()
    except click.exceptions.Abort:
        echo("[dim](aborted)[/]")
    except Exception as e:  # a command crash must not kill the session
        echo(f"[red]/{name} failed: {type(e).__name__}: {e}[/]")
    return None


def _sessions_cmd(c, loop, args, inp=None) -> None:
    """`/sessions` lists this workspace's saved sessions; `/sessions resume
    <id>` (or `--resume <id>`) loads one into the running conversation."""
    import time as _time

    from ..agent import Session
    store = str(c.ws.dot / "sessions")
    a = [x for x in args if x not in ("--resume", "resume")]
    asked_resume = "resume" in args or "--resume" in args
    if asked_resume and not a:  # `/sessions resume` with no id — don't silently list
        echo("[yellow]/sessions resume <id>[/] — give a session id "
             "(`/sessions` lists them)")
        return
    if (asked_resume or (a and a[0].startswith("S-"))) and a:
        sid = a[0]
        s = Session.load(sid, store)
        if s is None:
            echo(f"[yellow]no session '{sid}'[/] — /sessions lists them")
            return
        loop.session = s
        echo(f"[green]resumed[/] {sid} [dim]({len(s.messages)} messages) — the "
             f"next task continues with its context[/]")
        _replay_session(s)
        if inp is not None:  # make its requests recallable with up/down now
            inp.remember(_session_user_inputs(s), live=True)
        return
    rows = Session.summaries(store)
    if not rows:
        echo("[dim]no saved sessions in this workspace yet[/]")
        return
    echo("[bold]sessions[/] [dim](newest first — /sessions resume <id>)[/]")
    for r in rows:
        cur = " [green]● current[/]" if loop.session and r["id"] == loop.session.id else ""
        when = _time.strftime("%m-%d %H:%M", _time.localtime(r["mtime"]))
        echo(f"  [bold]{r['id']}[/]  [dim]{when} · {r['messages']} msg · "
             f"{r['target']}[/]{cur}")
        if r["preview"]:
            echo(f"      [dim]{r['preview']}[/]")


def _print_skills(c) -> None:
    from ..skills import discover_report, first_sentence
    rep = discover_report(c.ws)
    if not rep.skills:
        echo("[dim]no skills found — point a directory with[/] "
             "[bold]/skills add <dir>[/]")
        return
    hidden = []
    for s in rep.skills.values():
        # a skill named like a built-in can't own /<name>; say so here rather
        # than letting it look absent from the menu
        clash = s.name in cli.commands
        if clash:
            hidden.append(s.name)
        echo(f"  [bold]{s.name:24}[/] {first_sentence(s.description)[:76]} "
             f"[dim]({s.source})[/]")
    echo(f"[dim]{len(rep.skills)} skill(s) — /skill <name> loads one; the "
         f"agent also loads them itself via skill.use[/]")
    for n in hidden:
        echo(f"[yellow]  /{n} runs the built-in command[/] [dim]— use[/] "
             f"[bold]/skill:{n}[/] [dim]for the skill[/]")
    for w in rep.shadowed:
        echo(f"[yellow]  shadowed: {w}[/]")


def _load_skill(c, loop, name, pending) -> None:
    """Manual skill invocation: stage the playbook as context for the next
    plain-English task (loop.run takes one user string; prepending keeps the
    transcript and provider translation untouched)."""
    from .. import ui
    from ..skills import discover, skill_body
    sk = discover(c.ws)
    s = sk.get(name)
    if s is None:
        import difflib
        close = difflib.get_close_matches(name, list(sk), n=3, cutoff=0.5)
        echo(f"[yellow]unknown skill '{name}'[/]"
             + (f" — did you mean: {', '.join(close)}?" if close else ""))
        return
    body = skill_body(s)
    # manual path bypasses the loop's tool-result scanner — scan here (FR-SEC-05)
    try:
        from ..policy.injection import scan_text
        findings = scan_text(body["body"])
        if findings:
            ui.warn(f"  ⚠ injection-shaped content in skill '{name}' — "
                    f"loaded as data, policy unaffected")
    except Exception:
        pass
    n_lines = body["body"].count("\n") + 1
    if pending is None:
        # no session to stage into (e.g. no model): just show it
        ui.markdown(body["body"])
        return
    pending.append(f"[Skill playbook: {name} — follow where applicable]\n"
                   + body["body"])
    extra = f", {len(body['files'])} reference file(s)" if body["files"] else ""
    echo(f"[green]loaded skill '{name}'[/] [dim]({n_lines} lines{extra}) — "
         f"applies to your next message[/]")


_HELP_GROUPS = {
    "explore": ["hier", "module", "cone", "fsm", "cdc", "diagram",
                "index", "tools", "adapters", "packs"],
    "verify": ["sim", "lint", "lint-burndown", "cov", "triage", "ci", "bench"],
    "implement": ["uvm", "regmap", "synth", "optimize", "pnr", "fpga", "pins",
                  "efpga-fabulous", "riscv"],
    "report": ["jobs", "repro", "dashboard"],
    "setup": ["workspace", "library", "skills"],
}

# One hue per category, so the eye can jump to a section instead of reading
# every heading. Follows the flow of the work — look, prove, build, report —
# and reuses the meanings the rest of the UI already assigns: green passes
# (verify), yellow is the caution/config end (setup), cyan is the house accent.
_HELP_COLOR = {"explore": "cyan", "verify": "green", "implement": "magenta",
               "report": "blue", "setup": "yellow", "libraries": "blue",
               "session": "cyan", "more": "grey62", "skills": "#5f00af"}


def _head(name: str) -> str:
    """A /help section heading in its category color."""
    return f"[bold {_HELP_COLOR.get(name, 'cyan')}]{name}[/]"

# name -> "args — description", one per line. `\[` keeps rich from reading an
# argument hint like [on|off] as markup and swallowing it.
_HELP_SESSION = {
    "mode": " — cycle autonomy (shift-tab): normal · auto · plan",
    "theme": " — UI theme, live + saved: phosphor · amber · scope · mono · off",
    "plan": r" \[on|off] — plan mode (approve edits/jobs)",
    "sessions": r" \[resume <id>] — list or resume a saved session "
                "(also `chipchamp --continue` / `--resume <id>` at launch)",
    "skill": " <name> — load a skill playbook for the next task "
             "(or just /<skill-name>, /skill:<name> if a command owns it); "
             "/skill list shows them",
    "model": r" \[tune <key>=<value>] — switch model, or tune its "
             "generation params (num_ctx, temperature, top_p, seed, …)",
    "effort": r" \[low|medium|high|off|auto] — reasoning effort "
              "(reasoning models: gpt-oss, gpt-5, o-series; off pins, auto unpins)",
    "timeout": r" \[<seconds>|Nm|off|reset] — per-model call budget; raise it "
               "for a slow model (streaming: max gap between tokens)",
    "router": r" \[on|off|status|pool|chain|…] — auto model-routing across models",
    "undo": " — revert the files the last task changed "
            "(anything you edited by hand since is kept)",
    "clear": " — reset conversation",
    "help": " — this list",
    "exit": " — quit",
}


def _repl_help(c=None):
    """Print the command list, derived from the live command table.

    Every name the completion menu offers is shown: the curated groups first,
    then whatever they don't cover (new commands, discovered skills) under a
    catch-all — so the help can't silently drift out of date again."""
    cmds = _session_commands(c)

    def line(msg: str = "") -> None:
        """/help is hand-styled markup end to end, so the repr highlighter is
        off: it reads every /command as a filesystem path and paints it
        magenta, which drowns the category colors it would compete with."""
        echo(msg, highlight=False)

    line("[bold]Talk to the agent[/] — just type a task, e.g. "
         "[dim]“why does fifo_smoke fail on seed 3?”[/]")
    line("[bold]![/][dim]<command>[/] — run a shell command directly "
         "(plain terminal passthrough; never enters the session or the "
         "model's context)")
    shown = set()
    for g, names in _HELP_GROUPS.items():
        here = [n for n in names if n in cmds]
        shown.update(here)
        if here:
            line(f"{_head(g)}  " + " ".join(f"/{x}" for x in here))
    # registered IP libraries, each with its component count — so /library isn't
    # just a command name but shows what's actually available to reuse.
    if c is not None:
        try:
            from ..library import discover_report
            libs = discover_report(c.ws).libraries
            if libs:
                line(f"{_head('libraries')}  " + "  ".join(
                    f"{lib['name']} [dim]({lib['count']})[/]" for lib in libs))
        except Exception:
            pass  # a broken manifest must never break /help
    line(_head("session"))
    for name, tail in _HELP_SESSION.items():
        if name in cmds or name == "skill":
            shown.add(name)
            line(f"  /{name}{tail}")
    rest = [n for n in cmds if n not in shown]
    if rest:
        # skills discovered in the workspace are commands too; keep them apart
        # from real subcommands so the split stays meaningful. The table carries
        # the kind, so this matches what the completion menu shows.
        from .. import ui
        other = [n for n in rest if ui.menu_entry(cmds[n])[1] != "skill"]
        skills = [n for n in rest if ui.menu_entry(cmds[n])[1] == "skill"]
        if other:
            line(f"{_head('more')}  " + " ".join(f"/{x}" for x in other))
        if skills:
            line(f"{_head('skills')}  " + " ".join(f"/{x}" for x in skills))
    line("[dim]Any command also works from the shell: `chipchamp <command>`. "
         "Add --help to any /command for its options.[/]")
