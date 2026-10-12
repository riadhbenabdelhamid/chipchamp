"""Behavioral checks for the terminal controls and job inspection flow."""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace as NS

import pytest
from click.testing import CliRunner
from rich.console import Console

from chipchamp import ui, ui_preferences, ui_status
from chipchamp.cli import cli
from chipchamp.jobs.record import JobRecord, StepLog


@pytest.fixture(autouse=True)
def display(monkeypatch):
    # Each test owns its console and restores the application's globals.
    output = io.StringIO()
    monkeypatch.setattr(ui, "_console", Console(file=output, width=140, color_system=None))
    monkeypatch.setattr(ui, "_active_theme", "")
    monkeypatch.setattr(ui, "_density", "compact")
    monkeypatch.setattr(ui, "_motion", True)
    return output


def test_preferences_do_not_invalidate_verification(ctx):
    tracked = (ctx.ws.dot / "config.toml").read_bytes()
    before = ctx.verification_hash()
    ui_preferences.save(ctx.ws, "theme", "light")
    ui_preferences.save(ctx.ws, "motion", False)
    assert ui_preferences.load(ctx.ws) == {"theme": "light", "density": "compact", "motion": False}
    assert (ctx.ws.dot / "config.toml").read_bytes() == tracked
    assert ctx.verification_hash() == before


def test_preferences_override_project_and_recover_from_invalid_json(tmp_path):
    ws = NS(dot=tmp_path, config={"ui": {"theme": "amber", "density": "verbose"}})
    local = tmp_path / "ui.json"
    local.write_text('{"theme": "light", "density": 1, "motion": "no"}')
    assert ui_preferences.load(ws) == {"theme": "light", "density": "compact", "motion": True}
    local.write_text("{broken")
    assert ui_preferences.load(ws)["theme"] == "amber"
    ui_preferences.save(ws, "theme", "scope")
    assert json.loads(local.read_text()) == {"theme": "scope"}


def record(**kwargs):
    return JobRecord(**{"id": "J-1", "kind": "sim", "adapter": "icarus",
                       "target_name": "soc", "status": "passed", **kwargs})


def test_status_follows_active_target_and_source_edits(ctx):
    rec = record(target_name=ctx.target_name, verification_hash=ctx.verification_hash())
    other = record(id="J-2", target_name="another-target", status="failed")
    ctx.runner = NS(list_jobs=lambda: [rec, other])
    assert ui_status.job_states(ctx)["sim"] == "PASS"
    from pathlib import Path
    source = Path(ctx.target.sources[0])
    source.write_text(source.read_text() + "\n// input changed\n")
    ui_status.invalidate(ctx)
    assert ui_status.job_states(ctx)["sim"] == "STALE"
    assert ui_status.job_states(ctx)["lint"] == "NOT RUN"


def test_status_context_cache_and_expiry(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(ui_status.time, "monotonic", lambda: now[0])
    records = [record(verification_hash="current")]
    a = NS(target_name="soc", runner=NS(list_jobs=lambda: records), verification_hash=lambda: "current")
    b = NS(target_name="soc", runner=a.runner, verification_hash=lambda: "changed")
    assert ui_status.job_states(a)["sim"] == "PASS"
    assert ui_status.job_states(b)["sim"] == "STALE"
    records.append(record(id="J-2", status="running"))
    now[0] += 4
    assert ui_status.job_states(a)["sim"] == "RUNNING"
    a.target_name = "other"
    assert ui_status.job_states(a)["sim"] == "NOT RUN"


def test_status_without_provenance_is_unknown_and_snapshot_detects_changes(tmp_path):
    from chipchamp.util.hashing import sha256_file
    assert ui_status._record_state(record(), "current") == "UNKNOWN"
    source = tmp_path / "tb.sv"
    source.write_text("module tb; endmodule")
    rec = record(input_snapshot={"roots": [str(source)], "files": {str(source): sha256_file(str(source))}})
    assert ui_status._record_state(rec, "current") == "PASS"
    source.write_text("module tb; wire changed; endmodule")
    assert ui_status._record_state(rec, "current") == "STALE"


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
@pytest.mark.parametrize("burst,expected", [
    ("/job J-1 lo\r", "/job J-1 lo"),
    ("/lint\r", "/lint"),
    ("first\nsecond\r", "first\nsecond"),
    ("\x1b[200~first\nsecond\x1b[201~\r", "first\nsecond"),
])
def test_keyboard_submission_preserves_arguments_and_multiline(monkeypatch, burst, expected):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    monkeypatch.setattr(ui, "is_tty", lambda: True)

    async def drive():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            controller = ui.InputController({"lint-all": "everything", "lint": "one", "job": "inspect"},
                                            arguments=lambda text: [("log", "view log", 2)])
            pipe.send_text(burst)
            return await asyncio.wait_for(controller.session.prompt_async("> "), timeout=3)
    assert asyncio.run(drive()) == expected


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_menu_searches_descriptions_and_completes_arguments(ctx):
    from prompt_toolkit.document import Document
    from chipchamp.cli.completion import arguments
    menu = ui._SlashCompleter({"lint": "Check source diagnostics"}, lambda text: arguments(ctx, text))
    assert [c.text for c in menu.get_completions(Document("/diagnostics"), None)] == ["lint"]
    assert [c.text for c in menu.get_completions(Document("/theme li"), None)] == ["light"]
    name = ctx.ws.tests[0]["name"]
    assert name in [c[0] for c in arguments(ctx, "/sim ")]
    assert "--seed" in [c[0] for c in arguments(ctx, "/sim --s")]
    assert "--signal" in [c[0] for c in arguments(ctx, "/job J-1 --s")]
    assert not arguments(ctx, "/ui verbose ")


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
def test_prompt_palette_updates_live(monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.styles import merge_styles
    from prompt_toolkit.styles.defaults import default_ui_style
    from pygments.token import Keyword
    from chipchamp.ui_theme import palette, syntax_theme
    monkeypatch.setattr(ui, "is_tty", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        controller = ui.InputController({})
        for name in ("scope", "light", "phosphor", "mono"):
            ui.set_theme(name)
            accent = palette(name)["accent"]
            attrs = controller.session.style.get_attrs_for_style_str("class:prompt")
            assert attrs.color == accent.lstrip("#")
            assert syntax_theme(name).get_style_for_token(Keyword).color.triplet.hex == accent
            merged = merge_styles([default_ui_style(), controller.session.style])
            for field in ("completion", "meta.completion"):
                attrs = merged.get_attrs_for_style_str(f"class:completion-menu.{field}.current class:completion.skill")
                assert attrs.color != attrs.bgcolor
                assert attrs.bgcolor == accent.lstrip("#")


@pytest.mark.skipif(not ui._HAS_PTK, reason="prompt_toolkit not installed")
@pytest.mark.parametrize("width", [40, 80, 120])
def test_footer_fits_terminal_and_prioritizes_failures(monkeypatch, width):
    from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
    from rich.cells import cell_len
    from chipchamp.cli.repl import _board_leds
    monkeypatch.setattr(ui._console, "_width", width)
    monkeypatch.setattr(ui_status, "job_states", lambda c: {"lint": "PASS", "sim": "FAIL", "synth": "STALE"})
    c = NS(target_name="soc-with-a-long-target-name")
    loop = NS(gateway=NS(model="a-very-long-model-name-for-testing"))
    controller = ui.InputController({}, toolbar_extra=lambda: _board_leds(c, loop))
    text = fragment_list_to_text(to_formatted_text(controller._toolbar()))
    assert cell_len(text) <= width
    assert "sim FAIL" in text
    if width >= 80:
        assert ("class:status.error", "FAIL") in to_formatted_text(controller._toolbar())


def test_reduced_motion_disables_handoff_animation(monkeypatch, display):
    ui.configure({"theme": "scope", "motion": False})
    monkeypatch.setattr(ui, "is_tty", lambda: True)
    monkeypatch.setattr(ui.time, "sleep", lambda _: pytest.fail("animation should be disabled"))
    ui.handoff("from", "to", "reason")
    assert "router handoff" in display.getvalue()
    assert ui.pulse_glyph(0) == ui.pulse_glyph(20)


def test_failure_summary_preserves_literal_diagnostics_and_actions(display):
    rec = record(test_name="fifo_smoke", status="failed", artifacts={"waves": "dump.vcd"},
                 result={"diagnostics": [{"file": "rtl/fifo[0].sv", "line": 42,
                                          "message": "expected [red] but got [green]"}]})
    ui.job_result("sim.run", {"job": rec.id, "status": "failed"}, rec)
    text = display.getvalue()
    assert "FAIL" in text and "fifo_smoke" in text
    assert "rtl/fifo[0].sv:42" in text and "expected [red] but got [green]" in text
    for action in ("log", "waves", "rerun"):
        assert f"/job J-1 {action}" in text


def test_grouped_reads_keep_details_and_never_hide_errors(display):
    from chipchamp.cli.repl import _agent_events
    c = NS(runner=NS(), edits={})
    events = _agent_events(c)
    events.begin_task("inspect")
    for path in ("a.sv", "b.sv"):
        events("tool_call", {"name": "fs.read", "input": {"path": path}})
    assert not display.getvalue()
    events("tool_result", {"name": "fs.read", "result": {"error": "file [red] missing"}})
    assert "Read/query tools: 2 calls" in display.getvalue()
    assert "file [red] missing" in display.getvalue()
    assert [a["path"] for _, a in c._ui_call_details] == ["a.sv", "b.sv"]
    ui._density = "verbose"
    events("tool_call", {"name": "fs.read", "input": {"path": "c.sv"}})
    assert "c.sv" in display.getvalue()
    events.end_task()


def test_detached_job_completion_gets_a_final_verdict(display):
    from chipchamp.cli.repl import _agent_events
    rec = record(status="running")
    events = _agent_events(NS(runner=NS(get=lambda jid: rec), edits={}))
    events("tool_result", {"name": "sim.run", "result": {"job": rec.id, "status": "running"}})
    rec.status = "passed"
    result = {"name": "job.wait", "result": {"job": rec.id, "status": "passed"}}
    events("tool_result", result)
    events("tool_result", result)
    assert display.getvalue().count("PASS") == 1
    assert "RUNNING" in display.getvalue()


@pytest.fixture
def job_cli(monkeypatch, tmp_path):
    import chipchamp.cli.verify as verify
    rec = record(test_name="fifo_smoke", seed=0)
    c = NS(runner=NS(get=lambda jid: rec if jid == rec.id else None), target_name="soc",
           resolve_wave=lambda jid: None)
    monkeypatch.setattr(verify, "_ctx", lambda *a: c)
    def run(*args):
        return CliRunner().invoke(cli, ["--root", str(tmp_path), "--json", "job", *args])
    return c, rec, run, verify


def test_job_log_tails_across_steps_with_bounded_output(job_cli, tmp_path):
    _, rec, run, _ = job_cli
    for index, content in enumerate(("one\ntwo\n", "three\nfour\n")):
        path = tmp_path / f"step{index}.log"
        path.write_text(content)
        rec.steps.append(StepLog([], 0, 0, str(path)))
    result = run(rec.id, "log", "--lines", "3")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["text"] == "two\nthree\nfour\n"
    assert payload["truncated"]
    assert len(payload["paths"]) == 2
    result = run(rec.id, "log", "--lines", "2")
    assert json.loads(result.output)["truncated"]


def test_job_rerun_uses_policy_executor_and_preserves_seed_zero(job_cli, monkeypatch):
    c, rec, run, verify = job_cli
    calls = []
    def invoke(context, name, **args):
        calls.append((context, name, args))
        return {"job": "J-2", "status": "passed"}
    monkeypatch.setattr(verify, "_run_tool", invoke)
    result = run(rec.id, "rerun")
    assert result.exit_code == 0, result.output
    assert calls == [(c, "sim.run", {"test": "fifo_smoke", "seed": 0})]
    rec.target_name = "other"
    assert run(rec.id, "rerun").exit_code != 0
    assert len(calls) == 1


def test_job_missing_artifacts_and_ids_are_readable(job_cli):
    _, rec, run, _ = job_cli
    for args, message in ((("missing",), "No such job"), ((rec.id, "waves"), "No waveform available")):
        result = run(*args)
        assert result.exit_code != 0
        assert message in result.output


def test_job_wave_window_uses_recorded_trace(job_cli, tmp_path):
    from chipchamp.waves.store import WaveStore
    c, rec, run, _ = job_cli
    path = tmp_path / "trace.vcd"
    path.write_text("$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! clk $end\n"
                    "$upscope $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n#10\n0!\n")
    c.resolve_wave = lambda jid: WaveStore.open(str(path))
    result = run(rec.id, "waves", "--signal", "tb.clk", "--t0", "2", "--t1", "8")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["window"] == [2, 8]
    assert payload["signals"]["tb.clk"]["edges"] == [[5, "1"]]
    invalid = run(rec.id, "waves", "--t0", "9", "--t1", "2")
    assert invalid.exit_code != 0
    assert "0 <= t0 <= t1" in invalid.output


def test_job_wave_load_error_is_readable(job_cli):
    c, rec, run, _ = job_cli
    def broken(jid):
        raise RuntimeError("FST reader missing")
    c.resolve_wave = broken
    result = run(rec.id, "waves")
    assert result.exit_code != 0
    assert "Cannot load waveform: FST reader missing" in result.output
