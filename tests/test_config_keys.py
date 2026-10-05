"""Config keys and documented counts stay honest: unknown sections are
reported instead of silently ignored, [riscv] means what the README says,
tests.yaml defaults.seeds is read, and the README's numbers match the code."""
from __future__ import annotations

import re
from pathlib import Path

from chipchamp.config import KNOWN_SECTIONS, Workspace

README = Path(__file__).resolve().parent.parent / "README.md"


def _ws(tmp_path, toml):
    (tmp_path / ".chipchamp").mkdir(exist_ok=True)
    (tmp_path / ".chipchamp" / "config.toml").write_text(toml)
    return Workspace(str(tmp_path))


def test_unknown_sections_are_reported_not_ignored(tmp_path):
    ws = _ws(tmp_path, '[project]\nname="t"\n[waves]\nprefer="vcd"\n[budget]\nx=1\n')
    assert len(ws.config_warnings) == 2
    assert any("[waves]" in w for w in ws.config_warnings)
    assert any("[budget]" in w for w in ws.config_warnings)
    assert not _ws(tmp_path, '[project]\nname="t"\n[budgets.default]\ncpu_hours=1\n').config_warnings


def test_the_example_config_uses_only_known_sections():
    from conftest import REAL_EXAMPLE
    ws = Workspace(str(REAL_EXAMPLE))
    assert ws.config_warnings == [], ws.config_warnings
    assert set(ws.config) <= KNOWN_SECTIONS


def test_declaring_riscv_is_enough(tmp_path):
    ws = _ws(tmp_path, '[project]\nname="core"\n[riscv]\n')
    assert ws.policy_engine().riscv_core is True       # empty table still counts
    ws = _ws(tmp_path, '[project]\nname="soc"\n')
    assert ws.policy_engine().riscv_core is False


def test_tests_yaml_default_seeds_are_read(tmp_path):
    from types import SimpleNamespace as NS
    from chipchamp.tools.verif_tools import _seeds_for
    ws = _ws(tmp_path, '[project]\nname="t"\n')
    (tmp_path / ".chipchamp" / "tests.yaml").write_text("defaults:\n  seeds: [3, 5]\ntests: []\n")
    ws = Workspace(str(tmp_path))
    assert ws.test_defaults == {"seeds": [3, 5]}
    assert _seeds_for(NS(ws=ws), None) == [3, 5]
    assert _seeds_for(NS(ws=ws), [9]) == [9]
    assert _seeds_for(NS(ws=NS(test_defaults={})), None) == [1]


def test_readme_counts_match_the_code():
    from chipchamp.adapters.registry import _BUILTIN
    from chipchamp.tools import all_tools
    text = README.read_text()
    claimed = {int(n) for n in re.findall(r"\b(\d+) tools\b", text)}
    assert claimed == {len(all_tools())}, f"README says {claimed} tools; code has {len(all_tools())}"
    assert f"{len(_BUILTIN)} adapters" in text
