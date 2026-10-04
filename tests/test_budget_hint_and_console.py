"""Two small CLI truths: the budget approver names a key that exists, and the
CLI prints through the same console the theme is applied to."""
from __future__ import annotations

from types import SimpleNamespace as NS

from chipchamp import cli, ui
from chipchamp.policy.budgets import CONFIG_KEYS, config_hint


def test_every_ledger_kind_maps_to_a_real_config_key():
    from chipchamp.config import Workspace
    import inspect
    src = inspect.getsource(Workspace.budget)
    for kind, key in CONFIG_KEYS.items():
        assert f'"{key}"' in src, f"{kind} -> {key} is not read by Workspace.budget()"
    assert config_hint("v3_submissions") == "[budgets.default] v3_submissions_per_task"


def test_headless_budget_stop_names_section_and_key(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "warn", lambda m: seen.append(m))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    approve = cli._make_budget_approver(NS(ledger=NS(budget=None)))
    assert approve("v3_submissions", "V3 ceiling (3/3)") is False
    assert any("[budgets.default] v3_submissions_per_task" in m for m in seen)
    assert not any("raise [bold]v3_submissions[/]" in m for m in seen)


def test_cli_prints_through_the_themed_console():
    assert cli._con is ui.console()
    before = ui.set_theme("phosphor")
    try:
        assert cli._con is ui.console()          # theme pushed onto THIS console
    finally:
        ui.set_theme(before or "off")
