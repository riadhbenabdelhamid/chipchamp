"""Terminal UI rendering + input for the interactive session (SPEC §7.1).

Gives the REPL a Codex/Claude-Code feel:
  - agent responses rendered as Markdown,
  - SystemVerilog shown with syntax highlighting,
  - edits shown as colored unified diffs,
  - a live slash-command completion menu, a separated input area, and a
    clear-screen-on-entry so the session reads as its own app.

Everything degrades cleanly: rich renders plain when piped, and input falls back
to ``input()`` when there is no TTY (so scripted/piped use — and the test
suite — keep working). rich/pygments are hard deps; prompt_toolkit is optional
(``pip install 'chipchamp[tui]'``) — without it the slash menu falls back to
readline tab-completion.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
import threading
import time

from rich.console import Console
from rich.markdown import CodeBlock, Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

# Content sniff: does an untagged code fence look like SystemVerilog/Verilog?
# (Strong tokens only, so a shell/python snippet in a bare fence isn't miscolored.)
_SV_HINT = re.compile(
    r"\b(module|endmodule|always_ff|always_comb|always_latch|always|assign|"
    r"posedge|negedge|localparam|genvar|typedef\s+enum|initial\s+begin)\b"
    r"|`(define|timescale|include|ifdef)\b"
    r"|\b(logic|reg|wire)\s*(\[|\w+\s*[;,=])")


class _RTLCodeBlock(CodeBlock):
    """rich renders an untagged ``` fence as plain text. In an RTL tool the
    right default for untagged, SystemVerilog-looking code is the SV lexer."""

    @classmethod
    def create(cls, markdown, token):
        lexer = (token.info or "").partition(" ")[0]
        if lexer in ("", "text", "default", "plain"):
            lexer = "systemverilog" if _SV_HINT.search(token.content or "") else "text"
        return cls(lexer, markdown.code_theme)


class _RTLMarkdown(Markdown):
    elements = {**Markdown.elements,
                "fence": _RTLCodeBlock, "code_block": _RTLCodeBlock}

_console = Console()

# prompt_toolkit is optional
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app, run_in_terminal
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.enums import DEFAULT_BUFFER
    from prompt_toolkit.filters import has_completions, has_focus
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    _HAS_PTK = True
except Exception:  # pragma: no cover
    _HAS_PTK = False


def console() -> Console:
    return _console


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ---- output rendering --------------------------------------------------------


def clear() -> None:
    """Clear the screen so the session reads as its own app (TTY only)."""
    if is_tty():
        _console.clear()


def rule(label: str = "") -> None:
    _console.print(Rule(label, style="grey37", characters="─"))


def markdown(text: str) -> None:
    """Render agent output as Markdown (fenced code gets syntax highlighting)."""
    text = (text or "").strip()
    if not text:
        return
    try:
        _console.print(_RTLMarkdown(text, code_theme="ansi_dark"))
    except Exception:
        _console.print(text)


def _lang_for(path: str | None, lang: str | None) -> str:
    if lang:
        return lang
    if path:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".sv", ".svh", ".v", ".vh"):
            return "systemverilog"
        if ext in (".vhd", ".vhdl"):
            return "vhdl"
        if ext in (".py",):
            return "python"
        if ext in (".tcl",):
            return "tcl"
    return "systemverilog"


def source(text: str, path: str | None = None, lang: str | None = None,
           title: str | None = None, line_numbers: bool = True,
           start_line: int = 1) -> None:
    """Render source with syntax highlighting (SystemVerilog by default)."""
    syn = Syntax(text, _lang_for(path, lang), theme="ansi_dark",
                 line_numbers=line_numbers, start_line=start_line, word_wrap=False)
    if title or path:
        _console.print(Panel(syn, title=f"[cyan]{title or path}[/]",
                             border_style="grey37", title_align="left"))
    else:
        _console.print(syn)


def diff(old: str, new: str, path: str = "", *, context: int = 3,
         max_lines: int = 240) -> None:
    """Render a claude-code-style colored unified diff for an edit."""
    a = old.splitlines()
    b = new.splitlines()
    lines = list(difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}",
                                      n=context, lineterm=""))
    if not lines:
        _console.print(f"[dim]· {path}: no change[/]")
        return
    body = Text()
    added = removed = 0
    for ln in lines[:max_lines]:
        if ln.startswith("+++") or ln.startswith("---"):
            body.append(ln + "\n", style="dim")
        elif ln.startswith("@@"):
            body.append(ln + "\n", style="cyan")
        elif ln.startswith("+"):
            body.append(ln + "\n", style="green")
            added += 1
        elif ln.startswith("-"):
            body.append(ln + "\n", style="red")
            removed += 1
        else:
            body.append(ln + "\n", style="grey58")
    if len(lines) > max_lines:
        body.append(f"… (+{len(lines) - max_lines} more diff lines)\n", style="dim")
    title = f"[cyan]{path or 'edit'}[/]  [green]+{added}[/] [red]-{removed}[/]"
    _console.print(Panel(body, title=title, border_style="grey37", title_align="left"))


def _clk(i: int, width: int = 8) -> str:
    """A scrolling clock trace — the tool's heartbeat is literally a clock
    edge marching by (same timing-diagram language as the /effort waveform:
    `┌‾┐_┌‾┐_`). `i` is the frame counter; each frame advances one phase."""
    pat = "┌‾┐_"
    s = pat * (width // len(pat) + 2)
    off = (-i) % len(pat)  # trace scrolls right-to-left, like an analyzer
    return s[off:off + width]


# The verification ladder is the domain's universal mental model — every DV
# engineer already thinks in "am I past lint yet, is smoke clean". The platform
# has always computed it and only ever shown it at report.done, as a verdict.
# Shown continuously it becomes the session's spine: at any moment you can see
# which rung the work is on and what is still owed.
_GATE_MARK = {"pass": ("green", "✓"), "fail": ("red", "✗"),
              "missing": ("grey42", "·")}


def gate_ladder(report, width: int = 0) -> str:
    """`rtl_functional V3 · lint ✓ smoke_sim ✓ affected_regress · no_gaming ✓`.

    One line, because it repaints after every job and a block would push the
    conversation off screen. Gate names are kept verbatim rather than
    prettified — they are the same strings report.done will reject you with,
    and a HUD that renamed them would make that rejection unrecognizable."""
    if report is None or not getattr(report, "gates", None):
        return ""
    cells = []
    for g in report.gates:
        colour, mark = _GATE_MARK.get(g.status, ("grey42", "·"))
        cells.append(f"[{colour}]{mark}[/] [dim]{g.name}[/]")
    done = sum(1 for g in report.gates if g.ok)
    head = (f"[dim]ladder[/] [bold]{report.min_rung}[/] "
            f"[dim]{report.task_class}[/] "
            f"[{'green' if done == len(report.gates) else 'yellow'}]"
            f"{done}/{len(report.gates)}[/]")
    return head + "  " + "  ".join(cells)


def gate_fingerprint(report) -> str:
    """What the ladder looks like, for change detection. Repainting an
    identical ladder after every job would be noise, and noise is how a HUD
    stops being read."""
    if report is None or not getattr(report, "gates", None):
        return ""
    return "|".join(f"{g.name}={g.status}" for g in report.gates)


_PULSE = "✻✽✳✶✢·"


def pulse_glyph(i: int) -> str:
    """The animated sign of life. One glyph per 4 frames, so at the 0.1s frame
    rate it breathes (~2.5 Hz) instead of strobing."""
    return _PULSE[(i // 4) % len(_PULSE)]


def fmt_dur(sec: float) -> str:
    """38s · 4m 40s · 1h 07m — a running clock readable at a glance. Past a
    minute, raw seconds stop meaning anything."""
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def fmt_tok(n: float) -> str:
    """412 · 9.7k · 1.2M."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def task_label(text: str, width: int = 34) -> str:
    """A task's name for the status line: first line, whitespace collapsed,
    clipped. The user's own words rather than a generated verb — with several
    sessions open you should be able to tell which request is still running."""
    first = (text or "").strip().splitlines()
    s = " ".join(first[0].split()) if first else ""
    if not s:
        return "working"
    return s if len(s) <= width else s[:width - 1].rstrip() + "…"


class TaskPulse:
    """Task-scoped heartbeat: what was asked, when it started, how many tokens
    have come back.

    The per-step spinners come and go — one per model call, one per EDA job —
    but the clock and the token count belong to the whole task, so they live
    here and keep running across steps. Without this the timer would restart at
    every model call and read as though nothing had been happening."""

    def __init__(self, task: str = "", effort: str = ""):
        self.task = task_label(task)
        self.effort = effort or ""
        self.started = time.time()
        self.tokens = 0        # exact: committed by each finished model call

    def elapsed(self) -> float:
        return time.time() - self.started

    def add_tokens(self, n) -> None:
        try:
            self.tokens += max(0, int(n or 0))
        except (TypeError, ValueError):
            pass  # a provider that reports no usage must not break the spinner

    def status(self, live: int = 0, brief: bool = False) -> str:
        """`4m 40s · ↓ 9.7k tokens · thinking with xhigh effort`.

        `live` is the in-flight estimate for the call currently streaming —
        the only inexact part, which is why it puts a ~ on the count. `brief`
        drops the effort clause, the first thing to go on a narrow terminal."""
        tok = self.tokens + max(0, live)
        parts = [fmt_dur(self.elapsed()),
                 f"↓ {'~' if live else ''}{fmt_tok(tok)} tokens"]
        if self.effort and not brief:
            parts.append(f"thinking with {self.effort} effort")
        return " · ".join(parts)

    def line(self, i: int, live: int = 0, width: int = 0) -> str:
        """The whole sign of life, styled: `✽ <task>… (<status>)`.

        Degrades by importance as `width` tightens — the effort clause goes
        first, then the task name shrinks — because a hard clip would cut the
        line mid-word and lose the clock, which is the part that proves it is
        still alive."""
        glyph = pulse_glyph(i)
        # The effort clause is only worth its columns while the task name still
        # reads (>= NAMED chars). Without that floor the full status wins at 72
        # columns and leaves the name shorter than it gets at 56 — narrower
        # terminal, longer name.
        NAMED, MIN = 24, 8
        for status, floor in ((self.status(live), NAMED),
                              (self.status(live, brief=True), MIN)):
            # glyph + space + task + "… (" + status + ")"
            room = width - (len(glyph) + 4 + len(status)) if width else len(self.task)
            if room >= floor:
                task = task_label(self.task, room)
                dots = "" if task.endswith("…") else "…"
                return (f"\x1b[36m{glyph}\x1b[0m \x1b[2m{task}{dots} "
                        f"({status})\x1b[0m")
        # even that does not fit: the clock alone still shows it is working
        return f"\x1b[36m{glyph}\x1b[0m \x1b[2m{fmt_dur(self.elapsed())}\x1b[0m"


class Thinking:
    """An elapsed-time heartbeat shown while a call is in flight, so a slow
    (or wedged) wait is visibly *working* rather than looking hung. Animates on
    its own daemon thread (the caller is blocked in the call); a no-op off
    a TTY so piped/CI output stays clean. Clears its own line on stop."""

    def __init__(self, label: str = "thinking"):
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0

    def start(self) -> None:
        if not is_tty():
            return
        self._start = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            el = time.time() - self._start
            hint = "  [ctrl-c to interrupt]" if el >= 8 else ""
            sys.stdout.write(f"\r\x1b[36m{_clk(i)}\x1b[0m \x1b[2m{self.label} "
                             f"{el:0.0f}s{hint}\x1b[0m\x1b[K")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.1)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        sys.stdout.write("\r\x1b[2K")  # clear the spinner line
        sys.stdout.flush()
        self._thread = None


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _vislen(s: str) -> int:
    """Visible width of a string, ignoring ANSI escape sequences."""
    return len(_ANSI.sub("", s))


def _clip_ansi(s: str, width: int) -> str:
    """Truncate to `width` VISIBLE columns, keeping ANSI escapes (zero-width) and
    resetting styles at the cut — so a one-line status can never wrap (which
    would break the single-line repaint and stack spinners)."""
    if width <= 0 or _vislen(s) <= width:
        return s
    out, vis, i = [], 0, 0
    while i < len(s) and vis < width:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
        else:
            out.append(s[i])
            vis += 1
            i += 1
    return "".join(out) + "\x1b[0m"


def _close_fences(text: str) -> str:
    """A streaming markdown prefix can end inside an unclosed ``` fence — which
    would retroactively re-style everything after it as code. Balance it."""
    if text.count("```") % 2:
        return text + "\n```"
    return text


class StreamView:
    """Live view of a generating model turn.

    While only reasoning is arriving, shows a one-line ticker with the tail of
    the model's chain-of-thought scrolling by (dim) — a reasoning model looks
    *busy*, not hung. Once real content arrives, renders the accumulated text
    as Markdown progressively: lines that can no longer change are committed
    to normal scrollback; only the last few lines redraw each frame (so long
    answers never exceed the terminal's repaint region). `feed()` is called
    from the gateway's worker thread; rendering happens solely on this class's
    own thread — the only lock is around the accumulated buffers.

    Off a TTY everything no-ops and `streamed_chars` stays 0, which tells the
    caller to fall back to the one-shot Markdown render."""

    TAIL = 4  # volatile (redrawn) markdown lines at the bottom

    def __init__(self, label: str = "", pulse: "TaskPulse | None" = None):
        self.label = label
        self.pulse = pulse
        self.streamed_chars = 0
        self._content = ""
        self._reasoning = ""
        self._tool = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        self._committed = 0   # rendered lines already in normal scrollback
        self._painted = 0     # lines currently in the volatile repaint region
        self._render_console: Console | None = None

    # -- data ingress (any thread) --------------------------------------------

    def feed(self, channel: str, chunk: str) -> None:
        if self._thread is None:
            return
        with self._lock:
            if channel == "text":
                self._content += chunk
                self.streamed_chars += len(chunk)
            elif channel == "reasoning":
                self._reasoning += chunk
            elif channel == "tool":
                self._tool = chunk

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if not is_tty():
            return
        self._start = time.time()
        self._render_console = Console(width=_console.size.width,
                                       force_terminal=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        # final frame: erase the volatile region, print everything not yet
        # committed — the finished Markdown ends up in normal scrollback.
        self._erase()
        with self._lock:
            content = self._content
        if content.strip():
            lines = self._render(content)
            for ln in lines[self._committed:]:
                sys.stdout.write(ln + "\n")
        sys.stdout.flush()

    # -- rendering (own thread only) -------------------------------------------

    def _render(self, content: str) -> list[str]:
        con = self._render_console
        with con.capture() as cap:
            con.print(_RTLMarkdown(_close_fences(content),
                                   code_theme="ansi_dark"))
        return cap.get().split("\n")[:-1]  # drop the trailing empty split

    def _ticker(self, elapsed: float, i: int) -> str:
        with self._lock:
            tok = (len(self._content) + len(self._reasoning)) // 4
            tool = self._tool
            tail = self._reasoning[-160:]
        # Everything must fit ONE terminal row: the repaint erases a single line,
        # so a wrapped ticker leaves residue (stacked spinners). Budget the
        # remaining columns and clip the variable message to fit.
        width = max(30, _console.size.width - 1)
        if self.pulse is not None:
            head = self.pulse.line(i, live=tok, width=width)
        else:
            # no task context (headless, sub-agents): the model and its own
            # clock, which is all there is to report
            rate = f" · {tok / elapsed:0.0f} tok/s" if tok and elapsed >= 2 else ""
            head = (f"\x1b[36m{pulse_glyph(i)}\x1b[0m \x1b[2m{self.label} "
                    f"{fmt_dur(elapsed)} · ~{fmt_tok(tok)} tok{rate}\x1b[0m")
        parts = [head]
        used = _vislen(head)

        def add(text: str, *, from_end: bool = False) -> None:
            nonlocal used
            room = width - used - 1
            if room < 8:
                return
            if len(text) > room:
                text = ("…" + text[-(room - 1):]) if from_end else (text[:room - 1] + "…")
            parts.append(f"\x1b[2m{text}\x1b[0m")
            used += 1 + len(text)

        if tool:
            add(f"· composing {tool}")
        elif tok == 0 and elapsed >= 6:
            # no token yet: the model is almost certainly loading (a large local
            # model's first call pays the load + prefill cost). Say so, politely.
            add("· loading — large model, hang tight")
        elif tail.strip():
            add(f"▸ {' '.join(tail.split())}", from_end=True)
        if elapsed >= 8 and not tool:
            add("[ctrl-c to interrupt]")
        # final guard: even the prefix (a long model ref) can overrun a narrow
        # terminal — hard-clip so the row never wraps.
        return _clip_ansi(" ".join(parts), width)

    def _erase(self) -> None:
        if self._painted:
            sys.stdout.write(f"\r\x1b[{self._painted}A\x1b[J")
        else:
            sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
        self._painted = 0

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            with self._lock:
                content = self._content
            out = []
            volatile = 0
            if content.strip():
                lines = self._render(content)
                stable = max(self._committed, len(lines) - self.TAIL)
                commit = lines[self._committed:stable]
                tail = lines[stable:]
                out.extend(commit)
                self._committed = stable
                out.extend(tail)
                volatile = len(tail)
            out.append(self._ticker(time.time() - self._start, i))
            self._erase()
            sys.stdout.write("\n".join(out) + "\n")
            sys.stdout.flush()
            self._painted = volatile + 1
            i += 1
            self._stop.wait(0.1)


class JobTail:
    """Elapsed spinner + a rolling window of the job's live log — a running
    build/sim visibly *works* (compiler lines, sim cycles scrolling by)
    instead of blocking behind a dead spinner for minutes. The runner appends
    to `<job>/live.log` as output arrives; `attach()` hands the path over
    (from the submitting thread) once the job directory exists. Clears its
    whole block on stop; a TTY-only no-op otherwise."""

    def __init__(self, label: str, lines: int = 4,
                 pulse: "TaskPulse | None" = None):
        self.label = label
        self.lines = lines
        self.pulse = pulse
        self._path: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        self._painted = 0

    def attach(self, path: str) -> None:
        self._path = path  # str assignment is atomic; render picks it up

    def start(self) -> None:
        if not is_tty():
            return
        self._start = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        self._erase()

    def _erase(self) -> None:
        if self._painted:
            sys.stdout.write(f"\r\x1b[{self._painted}A\x1b[J")
            sys.stdout.flush()
        self._painted = 0

    def _tail(self) -> list[str]:
        if not self._path:
            return []
        try:
            with open(self._path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 16384))
                raw = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        width = max(20, _console.size.width - 4)
        out = [_ANSI.sub("", ln).rstrip()[:width]
               for ln in raw.splitlines() if ln.strip()]
        return out[-self.lines:]

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            el = time.time() - self._start
            # the task's own clock leads (a job is one step of it); the job's
            # label and its own elapsed sit underneath with the log tail
            width = max(30, _console.size.width - 1)
            job = f" \x1b[2m· {self.label} {fmt_dur(el)}\x1b[0m"
            head = (self.pulse.line(i, width=width - _vislen(job))
                    if self.pulse is not None
                    else f"\x1b[36m{pulse_glyph(i)}\x1b[0m")
            block = [_clip_ansi(head + job, width)]
            block += [f"\x1b[2m  │ {ln}\x1b[0m" for ln in self._tail()]
            self._erase()
            sys.stdout.write("\n".join(block) + "\n")
            sys.stdout.flush()
            self._painted = len(block)
            i += 1
            self._stop.wait(0.25)


_PLAN_MARK = {"done": "[green]✔[/]", "in_progress": "[cyan]▶[/]",
              "skipped": "[grey50]–[/]", "pending": "[grey50]○[/]"}

# pipeline-stage cells for the plan header: the plan IS a pipeline, beats
# (steps) moving through stages toward the output
_STAGE = {"done": "[green][✔][/]", "in_progress": "[bold cyan][▶][/]",
          "skipped": "[grey50][–][/]", "pending": "[grey50][ ][/]"}


def plan(steps: list[dict]) -> None:
    """Render the agent's todo checklist (updates in place across calls) with
    a pipeline-stage header: [✔]━[✔]━[▶]━[ ]  progress at a glance."""
    if not steps:
        return
    done = sum(1 for s in steps if s.get("status") == "done")
    pipe = "[grey37]━[/]".join(
        _STAGE.get(s.get("status", "pending"), _STAGE["pending"])
        for s in steps[:16])  # cap the trace so huge plans don't wrap
    if len(steps) > 16:
        pipe += "[grey37]━┄[/]"
    _console.print(f"[bold]Plan[/] {pipe} [dim]({done}/{len(steps)})[/]")
    for i, s in enumerate(steps, 1):
        st = s.get("status", "pending")
        mark = _PLAN_MARK.get(st, "[grey50]○[/]")
        text = s.get("step", "")
        style = "dim" if st in ("done", "skipped") else (
            "bold" if st == "in_progress" else "")
        body = f"[{style}]{text}[/]" if style else text
        _console.print(f"  {mark} [dim]{i}.[/] {body}")


def handoff(frm: str, to: str, reason: str) -> None:
    """Router model switch, rendered as what it is: a beat crossing a wire
    from the failing model to the fallback. Briefly animated on a TTY (~0.5s
    — the switch is a moment worth marking); always ends with a permanent
    static line so scrollback keeps the record."""
    if is_tty():
        span = 16
        a = frm.rsplit("/", 1)[-1]
        b = to.rsplit("/", 1)[-1]
        for i in range(span):
            wire = "─" * i + "●" + "─" * (span - 1 - i)
            sys.stdout.write(f"\r  \x1b[35m⇄\x1b[0m \x1b[2m{a}\x1b[0m "
                             f"\x1b[35m{wire}▶\x1b[0m \x1b[1m{b}\x1b[0m\x1b[K")
            sys.stdout.flush()
            time.sleep(0.03)
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
    _console.print(f"  [magenta]⇄ router handoff[/] [dim]{frm}[/] "
                   f"[magenta]━━▶[/] [bold]{to}[/] [dim]({reason})[/]")


def approval_request(name: str, desc: str) -> None:
    _console.print(f"  [yellow]⏸ approval needed[/] · [bold]{name}[/]  [dim]{desc}[/]")


def tool_call(name: str, args: dict | str) -> None:
    import json
    s = args if isinstance(args, str) else json.dumps(args)
    _console.print(f"  [grey50]→ {name}[/][grey37]({s[:80]})[/]")


def info(msg: str) -> None:
    _console.print(msg)


def warn(msg: str) -> None:
    _console.print(f"[yellow]{msg}[/]")


def error(msg: str) -> None:
    _console.print(f"[red]{msg}[/]")


# ---- input (slash menu + separated prompt) ----------------------------------

# A command table maps name -> one-line help. Entries that are NOT plain
# built-in commands carry a kind alongside the help, as (help, kind); the kind
# is what keeps a discovered skill from looking exactly like a subcommand.
#
# The tag has to do that work alone: the menu is a 4-row scrolling viewport
# (reserve_space_for_menu below), so sorting the skills into a block of their
# own would only push them past the fold — 40-odd commands come first
# alphabetically. Ordering stays alphabetical, which also keeps a skill next to
# the commands it shares a prefix with, where someone typing /cdc expects it.
_MENU_KINDS = {"skill": ("class:completion.skill", "skill · ")}


def menu_entry(value) -> tuple[str, str]:
    """Normalize a command-table value to (help, kind). A bare string is a
    built-in command — the baseline, deliberately left unstyled so the tagged
    kinds have something to contrast against."""
    if isinstance(value, str):
        return value, "command"
    desc, kind = value
    return desc, kind


if _HAS_PTK:
    class _SlashCompleter(Completer):
        def __init__(self, commands: dict):
            self.commands = commands

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            word = text[1:]
            # insertion order is the menu order — the table arrives sorted
            for name, value in self.commands.items():
                if not name.startswith(word):
                    continue
                desc, kind = menu_entry(value)
                style, badge = _MENU_KINDS.get(kind, ("", ""))
                # the badge leads the META column (not the name) so every
                # /name stays left-aligned for prefix typing
                yield Completion(name, start_position=-len(word),
                                 display=[(style, f"/{name}")],
                                 display_meta=[(style, badge), ("", desc)])


def _rig_direction_aware_menu(session, opens_up) -> None:
    """Teach a PromptSession's completion menu to open UPWARD (above the input
    line) when `opens_up()` is true — i.e. when the prompt sits in the lower
    half of the screen — instead of always dropping down.

    Three coordinated patches on the stock prompt layout:
      1. the below-the-cursor float menus hide while the drop-up is active;
      2. the input window stops reserving below-cursor rows in that case
         (no dead gap / pre-scroll when the menu will not open there);
      3. a CompletionsMenu is prepended ABOVE the whole prompt block, shown
         only while the drop-up is active — near the screen bottom it opens
         into the space above the input line, and the input line keeps its row.
    """
    from prompt_toolkit.filters import Condition, is_done
    from prompt_toolkit.layout import (CompletionsMenu, HSplit, Layout,
                                       MultiColumnCompletionsMenu, walk)
    from prompt_toolkit.layout.dimension import Dimension

    up = Condition(opens_up)
    root = session.app.layout.container
    for c in walk(root):                                  # (1)
        if isinstance(c, CompletionsMenu):
            c.filter = c.filter & ~up
        elif isinstance(c, MultiColumnCompletionsMenu):
            for ch in c.get_children():   # an HSplit; its rows hold the filter
                if hasattr(ch, "filter"):
                    ch.filter = ch.filter & ~up
    orig_h = session._get_default_buffer_control_height   # (2)
    for c in walk(root):
        if getattr(c, "height", None) == orig_h:
            c.height = lambda: Dimension() if opens_up() else orig_h()
    above = CompletionsMenu(                              # (3)
        max_height=session.reserve_space_for_menu or 4,
        scroll_offset=1, extra_filter=up & ~is_done)
    session.layout = session.app.layout = Layout(
        HSplit([above, root]),
        focused_element=session.app.layout.current_window)


class InputController:
    """Owns the prompt session. `commands` maps command name -> one-line help
    for the completion menu. `mode` (optional) is an autonomy-mode object with
    `.name` and `.cycle()`; when given, shift-tab cycles it and the current
    mode shows in the bottom toolbar."""

    def __init__(self, commands: dict[str, str], mode=None,
                 on_mode_change=None, toolbar_extra=None):
        self.commands = commands
        self.tty = is_tty()
        self.mode = mode
        self.on_mode_change = on_mode_change
        # up/down history is PER SESSION and in-memory: a new session starts
        # clean (no other sessions' commands bleeding in), while a resumed
        # session is seeded from its own transcript via remember(). Durable
        # history lives in the session file itself — resume it to get it back.
        self.history = None
        if _HAS_PTK and self.tty:
            try:
                self.history = InMemoryHistory()
            except Exception:
                self.history = None
        # optional callable -> prompt_toolkit HTML fragment, rendered at the
        # left of the toolbar (the dev-board LED strip). Crash-shielded: a
        # status-provider bug must never break the input prompt.
        self.toolbar_extra = toolbar_extra
        # True while the last scrollback line is our own mode announcement —
        # consecutive shift-tabs then overwrite it instead of stacking lines
        self._announced = False
        self.session = None
        if _HAS_PTK and self.tty:
            style = Style.from_dict({"prompt": "bold cyan",
                                     "bottom-toolbar": "bg:#222222 #888888",
                                     # skills in the slash menu. ptk's default
                                     # menu is LIGHT (#bbbbbb, #ffffff when
                                     # selected, #999999 for meta), so this has
                                     # to be a dark hue: violet clears 5:1 on
                                     # all three, a light accent clears none.
                                     "completion.skill": "#5f00af"})
            kb = KeyBindings()

            @kb.add("enter", filter=has_completions)
            def _(event):
                # the completion menu highlights its first entry by default
                # (see _preselect below); enter means "take the highlighted
                # command and run it" — /rev + enter behaves as /review
                buf = event.current_buffer
                cs = buf.complete_state
                if cs and cs.completions:
                    buf.apply_completion(cs.current_completion
                                         or cs.completions[0])
                buf.validate_and_handle()

            @kb.add("tab", filter=has_completions)
            def _(event):
                # first tab still applies the highlighted (first) entry —
                # without this, the preselected index would make tab skip
                # straight to the SECOND entry
                buf = event.current_buffer
                cs = buf.complete_state
                if cs and cs.completions and buf.document == cs.original_document:
                    buf.go_to_completion(cs.complete_index or 0)
                else:
                    buf.complete_next()

            @kb.add("enter", filter=~has_completions & has_focus(DEFAULT_BUFFER))
            def _(event):
                # fast typists can hit enter BEFORE the async completion round
                # lands ("/li<enter>" in one burst) — resolve the prefix
                # against the command table directly so the result is the
                # same as if the menu had been open
                buf = event.current_buffer
                text = buf.text
                if text.startswith("/") and " " not in text:
                    word = text[1:]
                    if word and word not in self.commands:
                        m = next((c for c in self.commands
                                  if c.startswith(word)), None)
                        if m:
                            buf.text = f"/{m}"
                            buf.cursor_position = len(buf.text)
                buf.validate_and_handle()

            if mode is not None:
                @kb.add("s-tab")  # shift-tab cycles the autonomy mode
                def _(event):
                    mode.cycle()
                    if self.on_mode_change:
                        # never print straight into a live ptk render — it
                        # corrupts the toolbar repaint. run_in_terminal
                        # suspends the renderer, prints, then repaints.
                        name = mode.name
                        run_in_terminal(lambda: self._announce_mode(name))
                    event.app.invalidate()  # redraw the toolbar
            self.session = PromptSession(
                completer=_SlashCompleter(commands), history=self.history,
                complete_while_typing=True, style=style, key_bindings=kb,
                # 4 menu rows (scrolls internally) instead of ptk's 8: less
                # dead space below the prompt in the drop-DOWN case; the
                # drop-up rig below removes it entirely in the lower half.
                reserve_space_for_menu=4)
            try:
                # lower half of the screen -> completion menu opens upward
                _rig_direction_aware_menu(self.session, self._menu_opens_up)
            except Exception:   # ptk internals moved: stock drop-down remains
                pass

            buf = self.session.default_buffer

            def _preselect(_b):
                # highlight the first menu entry as soon as completions land —
                # index only, no text applied, so typing keeps filtering
                cs = buf.complete_state
                if cs and cs.completions and cs.complete_index is None:
                    cs.go_to_index(0)

            buf.on_completions_changed += _preselect
        elif self.tty:
            self._init_readline()

    def _menu_opens_up(self) -> bool:
        """True when the prompt is in the lower half of the terminal. Uses the
        renderer's CPR-derived position; unknown height -> classic drop-down."""
        try:
            app = get_app()
            return (app.renderer.rows_above_layout
                    >= app.output.get_size().rows / 2)
        except Exception:
            return False

    def _announce_mode(self, name: str) -> None:
        """Print the one-line mode announcement. Consecutive announcements
        rewrite the previous line in place, so only the LATEST state shows."""
        if self._announced:
            sys.stdout.write("\x1b[1A\x1b[2K\r")
            sys.stdout.flush()
        self.on_mode_change(name)
        self._announced = True

    def _toolbar(self):
        extra = ""
        if self.toolbar_extra is not None:
            try:
                extra = self.toolbar_extra() or ""
            except Exception:
                extra = ""
        if extra:
            extra = f" {extra}  · "
        if self.mode is not None:
            color = getattr(self.mode, "color", "")  # per-mode accent (cli._MODE_COLOR)
            name = (f"<ansi{color}><b>{self.mode.name}</b></ansi{color}>"
                    if color else f"<b>{self.mode.name}</b>")
            return HTML(f"{extra} mode: {name} (shift-tab) "
                        f" ·  <b>/</b> commands  ·  ctrl-d to exit")
        return HTML(f"{extra} <b>/</b> commands  ·  enter to send  ·  "
                    "ctrl-d to exit")

    def _init_readline(self) -> None:
        try:
            import readline
            names = ["/" + c for c in self.commands]

            def completer(text, state):
                if not text.startswith("/"):
                    return None
                matches = [n for n in names if n.startswith(text)]
                return matches[state] + " " if state < len(matches) else None

            readline.set_completer(completer)
            readline.set_completer_delims(" \t\n")
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass

    def remember(self, strings, live: bool = False) -> None:
        """Seed prior inputs (oldest→newest) into up/down history, skipping any
        already present. Used to make a RESUMED session's own requests recallable
        — a new session is never seeded, so it stays clean. (`live` is accepted
        for call-site compatibility; the in-memory history is always immediate.)"""
        strings = [s.strip() for s in (strings or []) if s and s.strip()]
        if not strings:
            return
        if self.history is not None:
            try:
                have = set(self.history.get_strings())
            except Exception:
                have = set()
            for s in strings:
                if s in have:
                    continue
                have.add(s)
                self.history.append_string(s)
        elif self.tty:
            try:  # readline fallback keeps history in-process
                import readline
                have = {readline.get_history_item(i) for i in
                        range(1, readline.get_current_history_length() + 1)}
                for s in strings:
                    if s not in have:
                        readline.add_history(s)
            except Exception:
                pass

    def read(self) -> str:
        """Return a line; raises EOFError on ctrl-d, KeyboardInterrupt on ctrl-c."""
        self._announced = False  # anything printed since is not ours to erase
        if self.session is not None:
            return self.session.prompt(
                HTML("<prompt>» </prompt>"),
                bottom_toolbar=self._toolbar).strip()
        return input("» ").strip()

    def separator(self) -> None:
        """Visual separation of the input area (the 'line above' the prompt)."""
        if self.tty:
            _console.print(Rule(style="grey30", characters="─"))
