"""Terminal UI renderers + slash completion (SPEC §7.1 polish). Rendered to an
isolated non-tty Console so assertions are deterministic."""
from __future__ import annotations

import io

import pytest

from chipchamp import ui


def _capture(fn, *a, **k):
    from rich.console import Console
    buf = io.StringIO()
    old = ui._console
    ui._console = Console(file=buf, force_terminal=True, width=100, color_system="standard")
    try:
        fn(*a, **k)
    finally:
        ui._console = old
    return buf.getvalue()


def test_diff_colors_adds_and_removes():
    out = _capture(ui.diff, "logic a;\nassign x = mem[wptr];\n",
                   "logic a;\nassign x = mem[rptr];\n", "rtl/sync_fifo.sv")
    assert "sync_fifo.sv" in out
    assert "mem[wptr]" in out and "mem[rptr]" in out
    assert "\x1b[" in out  # ANSI color present (forced terminal)
    assert "+1" in out and "-1" in out  # add/remove counts in the title


def test_diff_no_change():
    out = _capture(ui.diff, "same\n", "same\n", "rtl/x.sv")
    assert "no change" in out


def test_source_highlights_systemverilog():
    code = "module m(input logic clk); always_ff @(posedge clk) q <= d; endmodule"
    out = _capture(ui.source, code, path="rtl/m.sv")
    assert "module" in out and "\x1b[" in out  # highlighted
    assert "rtl/m.sv" in out  # panel title


def test_markdown_renders_without_crash():
    out = _capture(ui.markdown, "# Title\n\nSome **bold** and `code`.\n\n```systemverilog\nassign x = y;\n```")
    assert "Title" in out and "assign" in out


def _ansi(s):
    import re
    return len(re.findall(r"\x1b\[[0-9;]*m", s))


def test_tagged_sv_fence_highlights():
    out = _capture(ui.markdown, "```systemverilog\nalways_ff @(posedge clk) q <= d;\n```")
    assert _ansi(out) > 2  # colored


def test_untagged_sv_looking_fence_highlights():
    # a bare ``` fence whose content looks like SV must still be highlighted
    md = "here is the fix:\n\n```\nalways_comb begin\n  next = state;\nend\n```"
    out = _capture(ui.markdown, md)
    assert _ansi(out) > 2, "untagged SystemVerilog fence was not highlighted"


def test_untagged_nonsv_fence_stays_plain():
    # a bare fence that is NOT SV must not be miscolored as Verilog
    md = "run this:\n\n```\ngit status && ls -la /tmp\n```"
    out = _capture(ui.markdown, md)
    assert _ansi(out) <= 2


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_slash_completer_menu():
    from prompt_toolkit.document import Document
    comp = ui._SlashCompleter({"sim": "run a test", "lint": "run lint",
                               "triage": "cluster failures"})
    # typing "/" offers every command
    got = [c.text for c in comp.get_completions(Document("/"), None)]
    assert {"sim", "lint", "triage"} <= set(got)
    # typing "/li" narrows to lint
    got = [c.text for c in comp.get_completions(Document("/li"), None)]
    assert got == ["lint"]
    # plain text offers nothing (goes to the agent)
    assert list(comp.get_completions(Document("why fail"), None)) == []


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_slash_completer_sets_skills_apart_from_commands():
    """A discovered skill and a built-in used to render identically. The name
    gets its own style and the meta column a badge — while the text that gets
    typed stays /<name>, so prefix completion is unaffected."""
    # a real pair that shares a prefix: /cdc ships as a command, cdc-audit is
    # a skill in tests/fixtures/skills — typing /cdc offers both
    from prompt_toolkit.document import Document
    comp = ui._SlashCompleter(
        {"cdc": "report clock-domain crossings",
         "cdc-audit": ("audit every crossing for a synchronizer", "skill")})
    got = {c.text: c for c in comp.get_completions(Document("/cdc"), None)}
    cmd, skill = got["cdc"], got["cdc-audit"]
    # the name column: unstyled for a command, tagged for a skill
    assert cmd.display_text == "/cdc" and skill.display_text == "/cdc-audit"
    assert not any("completion.skill" in f[0] for f in cmd.display)
    assert any("completion.skill" in f[0] for f in skill.display)
    # the meta column carries the badge, and only for the skill
    assert cmd.display_meta_text == "report clock-domain crossings"
    assert skill.display_meta_text == "skill · audit every crossing for a synchronizer"
    # what lands in the buffer is unchanged by any of it
    assert skill.text == "cdc-audit" and skill.start_position == -3


def test_menu_entry_normalizes_both_value_shapes():
    # a bare help string is a built-in; the pair form carries the kind
    assert ui.menu_entry("run lint") == ("run lint", "command")
    assert ui.menu_entry(("a playbook", "skill")) == ("a playbook", "skill")


def test_input_controller_builds():
    ic = ui.InputController({"sim": "run a test", "help": "show commands"})
    assert ic.commands["sim"] == "run a test"
    # non-tty in tests -> no prompt_toolkit session, read() uses input()
    assert ic.session is None or ic.tty is False


def test_mode_announce_shows_only_latest_state(capsys):
    # cycling shift-tab twice must leave ONE announcement line on screen:
    # the second announce erases the first (cursor-up + clear) in place
    calls = []
    ic = ui.InputController({}, on_mode_change=lambda n: calls.append(n))
    ic._announce_mode("auto")
    ic._announce_mode("plan")
    assert calls == ["auto", "plan"]
    out = capsys.readouterr().out
    assert out.count("\x1b[1A\x1b[2K") == 1  # only the 2nd call erases


def test_mode_announce_never_erases_foreign_output(monkeypatch, capsys):
    # after read() returns, other output owns the last line — a later
    # announcement must NOT cursor-up into it
    ic = ui.InputController({}, on_mode_change=lambda n: None)
    ic._announce_mode("auto")
    assert ic._announced
    monkeypatch.setattr("builtins.input", lambda prompt="": "next task")
    assert ic.read() == "next task"
    assert ic._announced is False            # reset: nothing to overwrite
    ic._announce_mode("plan")
    assert "\x1b[1A" not in capsys.readouterr().out.split("next task")[-1]


# ---- tier-2 liveness: clock heartbeat, pipeline plan, LEDs, burndown --------


def test_clk_trace_scrolls_and_stays_fixed_width():
    frames = [ui._clk(i, width=8) for i in range(6)]
    assert all(len(f) == 8 for f in frames)
    assert set("".join(frames)) <= set("┌‾┐_")
    assert frames[0] != frames[1]      # it moves
    assert frames[0] == frames[4]      # period 4: a clock, not noise


def _plain(s):
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_plan_renders_pipeline_header():
    steps = [{"step": "fetch components", "status": "done"},
             {"step": "write glue", "status": "in_progress"},
             {"step": "run sim", "status": "pending"}]
    out = _plain(_capture(ui.plan, steps))
    assert "[✔]" in out and "[▶]" in out and "[ ]" in out
    assert "━" in out                  # stages are connected
    assert "(1/3)" in out
    assert "write glue" in out         # checklist body retained


def test_plan_pipeline_caps_huge_plans():
    steps = [{"step": f"s{i}", "status": "pending"} for i in range(30)]
    out = _plain(_capture(ui.plan, steps))
    assert "┄" in out                  # truncation marker, no wrap explosion
    assert "(0/30)" in out


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_completion_preselects_first_and_enter_applies(monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    monkeypatch.setattr(ui, "is_tty", lambda: True)
    with create_pipe_input() as pipe, \
            create_app_session(input=pipe, output=DummyOutput()):
        ic = ui.InputController({"review": "review a PR", "regs": "regmap"})
        assert ic.session is not None
        # enter/tab are rebound for the has_completions case
        keys = {b.keys for b in ic.session.key_bindings.bindings}
        assert ("c-m",) in keys and ("c-i",) in keys
        import asyncio

        async def drive():                   # ptk fires invalidate handlers
            buf = ic.session.default_buffer  # that need a running loop
            buf.insert_text("/re")
            comps = list(ic.session.completer.get_completions(buf.document, None))
            assert [c.text for c in comps] == ["review", "regs"]
            cs = buf._set_completions(comps)  # completions land ->
            assert cs.complete_index == 0     # first entry preselected...
            assert buf.text == "/re"          # ...but typed text untouched
            # what the enter binding does: apply highlighted, then submit
            buf.apply_completion(cs.current_completion or cs.completions[0])
            assert buf.text == "/review"
        asyncio.run(drive())


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_enter_resolves_prefix_even_before_menu(monkeypatch):
    # "/li<enter>" typed in one burst: enter can beat the async completion
    # round — the fallback binding must still resolve to the first command
    import asyncio
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    monkeypatch.setattr(ui, "is_tty", lambda: True)

    async def run(burst):
        with create_pipe_input() as pipe, \
                create_app_session(input=pipe, output=DummyOutput()):
            ic = ui.InputController({"library": "libs", "lint": "lint"})
            pipe.send_text(burst)
            return await ic.session.prompt_async(HTML("<prompt>» </prompt>"))
    assert asyncio.run(run("/li\r")) == "/library"       # prefix -> first cmd
    assert asyncio.run(run("/lint x.sv\r")) == "/lint x.sv"  # args untouched
    assert asyncio.run(run("/xyz\r")) == "/xyz"          # no match untouched
    assert asyncio.run(run("hello\r")) == "hello"        # plain text untouched


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_direction_aware_menu_rig():
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout import CompletionsMenu, HSplit, walk
    from prompt_toolkit.output import DummyOutput
    flag = {"up": False}
    with create_pipe_input() as pipe:
        s = PromptSession(completer=ui._SlashCompleter({"sim": "x"}),
                          complete_while_typing=True,
                          input=pipe, output=DummyOutput(),
                          reserve_space_for_menu=4)

        def menus():
            return sum(1 for c in walk(s.app.layout.container)
                       if isinstance(c, CompletionsMenu))
        stock = menus()
        ui._rig_direction_aware_menu(s, lambda: flag["up"])
        root = s.app.layout.container
        # the drop-up menu is prepended above the stock prompt block
        assert isinstance(root, HSplit)
        assert isinstance(root.get_children()[0], CompletionsMenu)
        assert menus() == stock + 1
        # the below-the-input reservation exists for drop-down, not drop-up

        def reserved(up):
            flag["up"] = up
            best = 0
            for c in walk(root):
                h = getattr(c, "height", None)
                if callable(h):
                    try:
                        best = max(best, h().min or 0)
                    except Exception:
                        pass
            return best
        assert reserved(False) >= 4
        assert reserved(True) == 0


def test_mode_colors_distinct_and_complete():
    from chipchamp.cli import _MODE_COLOR, _MODE_DESC, _MODE_ORDER
    assert set(_MODE_COLOR) == set(_MODE_ORDER) == set(_MODE_DESC)
    assert len(set(_MODE_COLOR.values())) == len(_MODE_ORDER)  # all distinct


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_toolbar_colors_the_mode_name():
    from types import SimpleNamespace as NS
    # phosphor bar: the mode renders as an inverted phosphor-on-black pill
    # regardless of the legacy per-mode accent color
    for name in ("normal", "auto", "plan"):
        ic = ui.InputController({"sim": "x"}, mode=NS(name=name, color="cyan"))
        assert (f'<style bg="#000000" fg="#a8ff00"><b> {name} </b></style>'
                in ic._toolbar().value)
    ic = ui.InputController({"sim": "x"}, mode=NS(name="normal"))
    assert " normal " in ic._toolbar().value


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_toolbar_extra_is_crash_shielded():
    def boom():
        raise RuntimeError("status provider bug")
    ic = ui.InputController({"sim": "x"}, toolbar_extra=boom)
    text = ic._toolbar().value
    assert "commands" in text          # toolbar still renders
    ic2 = ui.InputController({"sim": "x"},
                             toolbar_extra=lambda: "<ansigreen>●</ansigreen>")
    assert "●" in ic2._toolbar().value


def test_board_leds_reflect_latest_job_per_kind():
    from types import SimpleNamespace as NS
    from chipchamp.cli import _board_leds
    _board_leds._cache = None          # defeat the TTL cache between tests

    class FakeRunner:
        def list_jobs(self):
            return [NS(kind="lint", status="failed"),
                    NS(kind="sim", status="failed"),
                    NS(kind="sim", status="passed"),   # later wins
                    NS(kind="formal", status="passed")]

    c = NS(runner=FakeRunner())
    loop = NS(gateway=NS(model="mistralai/devstral-small-2-2512"))
    out = _board_leds(c, loop)
    assert "lint" in out and "<ansired>●" in out       # failure = only color
    assert "sim ●" in out                              # latest passed: filled
    assert "synth" in out and "○" in out               # never ran
    assert "formal" in out                             # extra kind surfaced
    assert "devstral-small-2-2512" in out              # live model, short name
    _board_leds._cache = None


def test_board_leds_survive_a_broken_runner():
    from types import SimpleNamespace as NS
    from chipchamp.cli import _board_leds
    _board_leds._cache = None

    class DeadRunner:
        def list_jobs(self):
            raise OSError("runs dir vanished")

    out = _board_leds(NS(runner=DeadRunner()), NS(gateway=NS(model="m")))
    assert "lint" in out and "○" in out    # all dark, no crash
    _board_leds._cache = None


def test_burndown_sparkline():
    from chipchamp.cli import _burndown
    out = _burndown([8, 3, 0])
    assert "8" in out and "3" in out and "0" in out
    assert "[red]8" in out and "[green]0" in out
    assert any(ch in out for ch in "▁▂▃▄▅▆▇█")
    # long histories stay bounded
    assert _burndown(list(range(40, 0, -1))).count("▁") <= 12


# ---- tier-3 liveness: handoff, chip banner, failure waves --------------------


def test_handoff_prints_permanent_record_off_tty():
    # off-TTY: no animation loop, but the scrollback record must still print
    out = _plain(_capture(ui.handoff, "lmstudio:openai/gpt-oss-120b",
                          "lmstudio:mistralai/devstral-small-2-2512", "stall"))
    assert "router handoff" in out
    assert "gpt-oss-120b" in out and "devstral-small-2-2512" in out
    assert "(stall)" in out and "━━▶" in out


def test_chip_banner_silkscreen():
    from chipchamp.cli import _chip_banner
    lines = _chip_banner("run_router_qwen3", "/definitely/not/a/git/repo")
    assert len(lines) == 5
    text = " ".join(lines)
    assert "run_router_qw" in text           # project silkscreened (13 chars)
    assert "chipchamp" in text                # make + version stamped
    assert "lot" in text                     # lot code, silicon style
    assert "┌" in text and "┴" in text       # it looks like a package
    # a very long name must not blow the package width
    long = _chip_banner("a" * 60, "/nope")
    assert max(len(ln) for ln in long) <= max(len(ln) for ln in lines) + 2


def test_chip_banner_fill_and_ink_follow_the_package():
    """The package is filled, and the silkscreen tracks the fill's luminance —
    otherwise a light fill would print light ink on a light body."""
    from rich.text import Text

    from chipchamp.cli import _chip_banner, _chip_ink
    assert _chip_ink("grey27") == ("grey27", "grey62", "grey70", "bold cyan")
    fill, outline, ink, silk = _chip_ink("#d9d2c5")   # a light ceramic package
    assert (fill, outline, ink) == ("#d9d2c5", "grey11", "grey23")
    assert "005f87" in silk                          # dark name, not cyan
    assert _chip_ink("not-a-color")[0] == "grey27"   # never breaks the banner
    # every row carries the fill, so the body renders as a solid rectangle
    lines = _chip_banner("soc", "/x", fill="#0b3d2e")
    assert all("on #0b3d2e" in ln for ln in lines)
    # ...and the fill costs no columns: the banner lays chip and info side by
    # side on the visible width
    assert [Text.from_markup(l).cell_len for l in lines] == [22, 23, 23, 23, 22]


def test_banner_highlights_the_info_but_not_the_chip(monkeypatch):
    """Two halves on one line, highlighted differently: the chip is hand-styled
    so the repr highlighter must not recolor its version digits, while the text
    beside it keeps the highlighting it has always had."""
    import io
    from types import SimpleNamespace as NS

    from rich.console import Console

    from chipchamp import __version__, cli
    con = Console(file=io.StringIO(), force_terminal=True,
                  color_system="truecolor", width=110)
    monkeypatch.setattr(cli, "_con", con)
    ws = NS(config={"project": {"name": "soc_top"}}, root="/repo/path",
            default_target="t", db=lambda: NS(modules={"a": 1, "b": 2}))
    cli._print_banner(NS(ws=ws, target_name="t"),
                      NS(ref="anthropic:claude-sonnet-5"), True, "", 0)
    out = con.file.getvalue()
    # chip: the silkscreen is one unbroken span (highlighted, "0.1" and "0"
    # would be bolded separately and split it)
    assert f"chipchamp {__version__}" in out
    # info: the highlighter still runs there — it splits the repo path into
    # its directory and basename colors, so the plain string is NOT contiguous
    assert "/repo/path" not in out
    assert "\x1b[35m/repo/" in out and "\x1b[95mpath" in out


def test_failure_waves_hook_is_silent_on_any_error():
    from types import SimpleNamespace as NS
    from chipchamp.cli import _print_failure_waves

    class Boom:
        def resolve_wave(self, jid):
            raise OSError("no such wave")

    _print_failure_waves(Boom(), "J-0001")          # raising resolver
    _print_failure_waves(NS(resolve_wave=lambda j: None), "J-0002")  # no store


def test_stream_ticker_never_exceeds_terminal_width():
    """The generating-turn ticker is one repaint-erased row; if it wraps, the
    single-line erase leaves residue and spinners stack (the loading-message
    bug). Its visible width must stay within the terminal."""
    from chipchamp import ui
    width = ui._console.size.width
    sv = ui.StreamView("ollama:laguna-s-2.1:latest")   # a long label, like a real ref
    # cold-load frame (no tokens yet, elapsed past the patience threshold)
    assert ui._vislen(sv._ticker(12.0, 3)) <= width
    # a long reasoning tail must be clipped, not wrapped
    sv._reasoning = "thinking out loud " * 100
    assert ui._vislen(sv._ticker(20.0, 5)) <= width
    # ctrl-c hint frame
    assert ui._vislen(sv._ticker(30.0, 7)) <= width
