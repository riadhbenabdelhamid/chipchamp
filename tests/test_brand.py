"""Branding (chipchamp.brand): the app name is ONE settable global and every
surface derives from it — env vars, testbench markers, workspace dot-dirs.

This repository has only ever shipped as chipchamp, so ``LEGACY_NAMES`` is
empty. The compatibility *plumbing* is still tested, because it is what a
future rename will rest on: these tests inject a hypothetical earlier era
rather than naming a dead brand, so they keep their meaning no matter what the
product was or will be called.
"""
from __future__ import annotations

import pytest

from chipchamp import brand


@pytest.fixture
def restore_brand():
    yield
    brand.set_name("chipchamp")


@pytest.fixture
def with_legacy_era(monkeypatch):
    """Pretend the product once shipped as `oldname`."""
    monkeypatch.setattr(brand, "LEGACY_NAMES", ("oldname",))
    return "oldname"


# ---- the current brand ------------------------------------------------------

def test_set_name_rebrands_every_derived_surface(restore_brand):
    brand.set_name("myfab")
    assert brand.APP_NAME == "myfab"
    assert brand.env_name("MODEL_TIMEOUT") == "MYFAB_MODEL_TIMEOUT"
    assert brand.marker("PASS") == "MYFAB_PASS"
    assert brand.dot_dir_names()[0] == ".myfab"
    # the SHIPPED default stays accepted after a rebrand, so a set_name() call
    # cannot orphan artifacts this build already wrote
    assert "CHIPCHAMP_PASS" in brand.markers("PASS")
    brand.set_name("")          # empty is ignored, not a wipe
    assert brand.APP_NAME == "myfab"


def test_ships_with_no_legacy_eras():
    """A fresh repository has nothing to be compatible with. If this ever
    fails, a rename happened and the compat tests below became live."""
    assert brand.LEGACY_NAMES == ()
    assert brand.all_names() == ("chipchamp",)
    assert brand.markers("PASS") == ("CHIPCHAMP_PASS",)
    assert brand.dot_dir_names() == (".chipchamp",)


def test_env_reads_the_current_brand(monkeypatch):
    monkeypatch.setenv("CHIPCHAMP_MODEL_TIMEOUT", "222")
    assert brand.env("MODEL_TIMEOUT") == "222"
    assert brand.env("NOT_SET_ANYWHERE", "dflt") == "dflt"


# ---- the seam a future rename will rest on ---------------------------------

def test_an_earlier_eras_env_var_still_resolves(with_legacy_era, monkeypatch):
    monkeypatch.delenv("CHIPCHAMP_MODEL_TIMEOUT", raising=False)
    monkeypatch.setenv("OLDNAME_MODEL_TIMEOUT", "111")
    assert brand.env("MODEL_TIMEOUT") == "111"          # fallback honoured
    monkeypatch.setenv("CHIPCHAMP_MODEL_TIMEOUT", "222")
    assert brand.env("MODEL_TIMEOUT") == "222"          # current wins


def test_markers_accept_every_era_current_first(with_legacy_era):
    assert brand.markers("PASS") == ("CHIPCHAMP_PASS", "OLDNAME_PASS")


def test_sim_parser_accepts_every_eras_markers(with_legacy_era):
    """A harness generated before a rename must still verify — the whole point
    of the compat layer."""
    from chipchamp.adapters.base import StepResult
    from chipchamp.adapters.icarus import _status_from_log
    ok = StepResult(argv=[], rc=0, stdout="", stderr="")
    assert _status_from_log("blah\nOLDNAME_PASS\n", ok)[0] == "pass"
    assert _status_from_log("blah\nCHIPCHAMP_PASS\n", ok)[0] == "pass"
    st, msg = _status_from_log("OLDNAME_FAIL: cycle 3 mismatch\n", ok)
    assert st == "fail" and "cycle 3 mismatch" in msg
    st, msg = _status_from_log("CHIPCHAMP_FAIL: bad data\n", ok)
    assert st == "fail" and "bad data" in msg


def test_dot_dir_prefers_config_over_bare_current_name(with_legacy_era, tmp_path):
    """The workspace machinery mkdirs under the dot-dir, so a spawned-empty
    .chipchamp must NOT shadow an earlier era's dir holding the real config."""
    (tmp_path / ".chipchamp" / "runs").mkdir(parents=True)   # accidental shell
    legacy = tmp_path / ".oldname"
    legacy.mkdir()
    (legacy / "config.toml").write_text("[project]\nname='x'\n")
    assert brand.dot_dir(tmp_path).name == ".oldname"
    # once the CURRENT dir holds config, it wins
    (tmp_path / ".chipchamp" / "config.toml").write_text("[project]\n")
    assert brand.dot_dir(tmp_path).name == ".chipchamp"


def test_dot_dir_fresh_workspace_uses_the_current_brand(tmp_path):
    assert brand.dot_dir(tmp_path).name == ".chipchamp"


def test_workspace_uses_an_earlier_eras_dot_dir(with_legacy_era, tmp_path):
    from chipchamp.config import Workspace
    dot = tmp_path / ".oldname"
    dot.mkdir()
    (dot / "config.toml").write_text("[project]\nname = 'legacy'\n")
    ws = Workspace(str(tmp_path))
    assert ws.dot.name == ".oldname"
    assert ws.config.get("project", {}).get("name") == "legacy"


def test_policy_protection_covers_every_era(with_legacy_era, tmp_path):
    """A protected list written under one era must also shield the other's
    path — a rename can't open a hole in what the agent may edit."""
    import yaml
    dot = tmp_path / ".oldname"
    dot.mkdir()
    (dot / "config.toml").write_text("[project]\nname = 'p'\n")
    (dot / "policy.yaml").write_text(yaml.safe_dump(
        {"protected": [".oldname/policy.yaml", "waivers/**"]}))
    from chipchamp.config import Workspace
    hits = Workspace(str(tmp_path)).policy_engine().protected_paths
    assert ".oldname/policy.yaml" in hits
    assert ".chipchamp/policy.yaml" in hits


def test_waves_defines_cover_every_era(with_legacy_era):
    from chipchamp.adapters.icarus import IcarusAdapter
    argv = IcarusAdapter().sim(["tb.sv"], "tb", "/tmp", waves=True).steps[0].argv
    for era in ("CHIPCHAMP", "OLDNAME"):
        assert f"-D{era}_WAVES" in argv, era


def test_pinned_root_candidates_follow_the_brand(with_legacy_era):
    """The pin file is looked up per era too, derived from the brand rather
    than a hand-kept constant — one fewer place a rename must touch."""
    from chipchamp.config import _pinned_root_files
    names = [p.parent.name for p in _pinned_root_files()]
    assert names == ["chipchamp", "oldname"]
