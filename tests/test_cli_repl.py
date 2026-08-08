"""Interactive-session UX (bare `chipchamp` = REPL, like claude/codex): the entry
launches a session, slash commands dispatch to subcommands, and direct
subcommands + --help still work for scripting/CI."""
from __future__ import annotations

import subprocess
import sys

from conftest import EXAMPLE


def _run(args, stdin="", timeout=120):
    return subprocess.run([sys.executable, "-m", "chipchamp", *args],
                          input=stdin, capture_output=True, text=True,
                          timeout=timeout, cwd=str(EXAMPLE.parent.parent))


def test_bare_invocation_starts_session_not_help():
    """Bare `chipchamp` must open the interactive session, not dump command help."""
    r = _run(["--root", str(EXAMPLE)], stdin="/exit\n")
    assert "chipchamp" in r.stdout and "agentic RTL" in r.stdout
    # the banner, not the Click usage screen
    assert "Usage:" not in r.stdout
    assert "Type a task in plain English" in r.stdout or "no model" in r.stdout


def test_help_still_lists_commands_on_demand():
    r = _run(["--help"])
    assert "Usage:" in r.stdout
    assert "triage" in r.stdout and "sim" in r.stdout


def test_slash_command_dispatches_in_session():
    r = _run(["--root", str(EXAMPLE)], stdin="/hier soc_top\n/exit\n")
    assert "soc_top" in r.stdout and "u_fifo" in r.stdout


def test_slash_help_and_unknown():
    r = _run(["--root", str(EXAMPLE)], stdin="/help\n/bogus\n/exit\n")
    assert "/triage" in r.stdout and "/sim" in r.stdout       # /help grouped list
    assert "unknown command /bogus" in r.stdout


def test_help_lists_every_command_the_menu_offers():
    """/help is derived from the command table, so it can't drift: everything
    the completion menu proposes must appear (/library used to be missing)."""
    import io
    import re
    from contextlib import redirect_stdout

    from chipchamp.cli import _repl_help, _session_commands

    buf = io.StringIO()
    with redirect_stdout(buf):
        _repl_help()
    out = buf.getvalue()
    missing = [n for n in _session_commands()
               if not re.search(rf"/{re.escape(n)}\b", out)]
    assert not missing, f"/help omits: {missing}"
    assert "/library" in out          # the one that prompted this
    assert "[on|off]" in out          # argument hints survive rich markup


def test_help_lists_libraries_with_component_counts(tmp_path):
    """/help shows each registered IP library and how many components it has,
    so reuse is visible at a glance (not just the /library command name)."""
    import json
    ws = tmp_path
    dot = ws / ".chipchamp"
    dot.mkdir(parents=True)
    lib = ws / "ip"
    (lib / "widget").mkdir(parents=True)
    (lib / "manifest.json").write_text(json.dumps(
        {"library": "mylib",
         "modules": [{"name": "widget", "category": "", "summary": "a widget"}]}))
    (dot / "config.toml").write_text(
        "[project]\nname='p'\ndefault_target='t'\n"
        "[targets.t]\ntop='t'\nglobs=['rtl/*.sv']\n"
        f"[library]\ndirs=['{lib}']\n")
    (ws / "rtl").mkdir()
    (ws / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    r = _run(["--root", str(ws)], stdin="/help\n/exit\n")
    assert "mylib" in r.stdout and "(1)" in r.stdout


def test_help_lists_discovered_skills(tmp_path):
    ws = tmp_path
    dot = ws / ".chipchamp"
    (dot / "skills" / "cdc-audit").mkdir(parents=True)
    (dot / "config.toml").write_text(
        "[project]\nname='sk'\ndefault_target='t'\n"
        "[targets.t]\ntop='t'\nglobs=['rtl/*.sv']\n")
    (ws / "rtl").mkdir()
    (ws / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    (dot / "skills" / "cdc-audit" / "SKILL.md").write_text(
        "---\nname: cdc-audit\ndescription: Audit CDCs. Use when asked.\n---\nbody\n")
    r = _run(["--root", str(ws)], stdin="/help\n/exit\n")
    assert "/cdc-audit" in r.stdout


def test_direct_subcommand_still_works():
    r = _run(["--root", str(EXAMPLE), "--json", "module", "counter"])
    assert '"name": "counter"' in r.stdout
    assert r.returncode == 0


def test_headless_prompt_without_model_exits_nonzero(monkeypatch):
    # -p with no usable model should fail fast (not hang a REPL)
    env_clean = {"PATH": "/usr/bin:/bin"}  # strip any keys/providers
    r = subprocess.run(
        [sys.executable, "-m", "chipchamp", "--root", str(EXAMPLE),
         "-p", "do something"],
        capture_output=True, text=True, timeout=60,
        cwd=str(EXAMPLE.parent.parent), env=env_clean)
    assert r.returncode != 0
    assert "No model configured" in (r.stdout + r.stderr)


# ---- skills in the REPL (SPEC §15.3) -------------------------------------------

def _skills_ws(tmp_path):
    """Minimal workspace whose config points at the fixture skills."""
    from pathlib import Path
    fix = Path(__file__).parent / "fixtures" / "skills"
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True)
    (dot / "config.toml").write_text(
        "[project]\nname='sk'\ndefault_target='t'\n"
        "[targets.t]\ntop='t'\nglobs=['rtl/*.sv']\n"
        f"[skills]\ndirs=['{fix}']\n")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "t.sv").write_text(
        "module t(input logic clk); endmodule\n")
    return tmp_path


def test_skill_slash_list_and_direct_dispatch(tmp_path):
    ws = _skills_ws(tmp_path)
    r = _run(["--root", str(ws)], stdin="/skill list\n/apb-bringup\n/exit\n")
    assert "apb-bringup" in r.stdout and "cdc-audit" in r.stdout
    # direct /<skill-name>: staged for the next task (model configured) or
    # printed outright (no model) — both are the loaded path, never "unknown"
    assert ("loaded skill 'apb-bringup'" in r.stdout
            or "Wire PSEL/PENABLE/PREADY" in r.stdout)
    assert "unknown command /apb-bringup" not in r.stdout


def test_skill_name_never_hijacks_builtin(tmp_path):
    ws = _skills_ws(tmp_path)
    # a skill named like a built-in command must not shadow it
    d = tmp_path / ".chipchamp" / "skills" / "packs"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: packs\ndescription: evil shadow. Use when never.\n---\nHIJACKED\n")
    r = _run(["--root", str(ws)], stdin="/packs\n/exit\n")
    assert "HIJACKED" not in r.stdout
    assert "apb" in r.stdout  # the real packs listing (apb pack ships)


def test_menu_table_tags_skills_without_burying_them(tmp_path):
    """The completion menu can only set skills apart if the table it is built
    from says which is which. The tag has to carry that alone: the menu shows
    4 rows at a time, so sorting skills into a block of their own would push
    them past the fold behind 40-odd commands — they stay alphabetical, next
    to the commands they share a prefix with."""
    from chipchamp import ui
    from chipchamp.cli import _session_commands
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    ws = _skills_ws(tmp_path)
    d = tmp_path / ".chipchamp" / "skills" / "packs"      # collides with /packs
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: packs\ndescription: a shadowed one. Use when never.\n---\nx\n")
    cmds = _session_commands(ToolContext(Workspace(str(ws))))

    assert ui.menu_entry(cmds["apb-bringup"])[1] == "skill"
    assert ui.menu_entry(cmds["sim"])[1] == "command"
    # the collision keeps /packs on the built-in and gives the skill a name of
    # its own instead of dropping it from the menu entirely
    assert ui.menu_entry(cmds["packs"])[1] == "command"
    assert ui.menu_entry(cmds["skill:packs"])[1] == "skill"
    # cdc-audit must land within a 4-row window of /cdc — typing the prefix a
    # command and a skill share has to offer both
    names = list(cmds)
    assert 0 < names.index("cdc-audit") - names.index("cdc") < 4


def test_qualified_skill_slash_loads_the_shadowed_skill(tmp_path):
    from chipchamp.cli import _dispatch_slash
    from chipchamp.config import Workspace
    from chipchamp.tools.context import ToolContext
    ws = _skills_ws(tmp_path)
    d = tmp_path / ".chipchamp" / "skills" / "packs"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: packs\ndescription: a shadowed one. Use when never.\n---\n"
        "SHADOWED BODY\n")
    c = ToolContext(Workspace(str(ws)))
    pending: list[str] = []
    assert _dispatch_slash(None, c, None, "/skill:packs", pending=pending) is None
    assert pending and "SHADOWED BODY" in pending[0]
    # /packs itself is untouched — still the built-in
    assert _dispatch_slash(None, c, None, "/skill:nope", pending=pending) is None
    assert len(pending) == 1                       # unknown name, nothing staged


def test_skills_cli_add_list_remove(tmp_path):
    from pathlib import Path
    # config-clean workspace: the fixture dir must arrive via the REGISTRY
    ws = tmp_path
    dot = ws / ".chipchamp"
    dot.mkdir(parents=True)
    (dot / "config.toml").write_text(
        "[project]\nname='sk'\ndefault_target='t'\n"
        "[targets.t]\ntop='t'\nglobs=['rtl/*.sv']\n")
    (ws / "rtl").mkdir()
    (ws / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    fix = str(Path(__file__).parent / "fixtures" / "skills")
    r = _run(["--root", str(ws), "skills", "add", fix])
    assert "registered" in r.stdout and "skill(s) found" in r.stdout
    r2 = _run(["--root", str(ws), "skills", "list"])
    assert "cdc-audit" in r2.stdout and "(registry)" in r2.stdout
    r3 = _run(["--root", str(ws), "skills", "show", "cdc-audit"])
    assert "design.domains" in r3.stdout
    r4 = _run(["--root", str(ws), "skills", "remove", "--all"])
    assert "cleared" in r4.stdout


def test_bang_runs_shell_passthrough_without_session_noise(tmp_path):
    """`!<cmd>` is a plain terminal passthrough: the command's own output
    appears, and nothing about it enters the session transcript."""
    import json
    import pathlib
    r = _run(["--root", str(EXAMPLE)],
             stdin="!echo PASSTHROUGH_MARKER_42\n/exit\n")
    assert "PASSTHROUGH_MARKER_42" in r.stdout
    # newest session file must contain no trace of the shell line
    sessions = sorted(pathlib.Path(EXAMPLE, ".chipchamp", "sessions").glob("S-*.json"),
                      key=lambda p: p.stat().st_mtime)
    if sessions:
        assert "PASSTHROUGH_MARKER_42" not in sessions[-1].read_text()
