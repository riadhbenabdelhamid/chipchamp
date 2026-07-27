"""Working-repo resolution (launch from any repo): auto-detect, pin, override."""
from __future__ import annotations

import os
import subprocess

import pytest

import chipchamp.config as cfg


@pytest.fixture
def isolated_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(cfg, "_PINNED_ROOT_FILE", tmp_path / "cfg" / "root")
    for var in ("CHIPCHAMP_ROOT",):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_find_project_root_walks_up_to_dot_dir(tmp_path):
    proj = tmp_path / "chip"
    (proj / ".chipchamp").mkdir(parents=True)
    deep = proj / "rtl" / "dma"
    deep.mkdir(parents=True)
    assert cfg.find_project_root(str(deep)) == str(proj)


def test_find_project_root_falls_back_to_git_toplevel(tmp_path):
    repo = tmp_path / "gitrepo"
    (repo / "rtl").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert cfg.find_project_root(str(repo / "rtl")) == str(repo.resolve())


def test_find_project_root_plain_dir_is_itself(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert cfg.find_project_root(str(d)) == str(d.resolve())


def test_resolve_precedence(isolated_pin, tmp_path, monkeypatch):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    c = tmp_path / "c"; c.mkdir()
    # 1. explicit --root wins over everything
    monkeypatch.setenv("CHIPCHAMP_ROOT", str(b))
    cfg.pin_root(str(c))
    root, how = cfg.resolve_root(str(a))
    assert root == str(a.resolve()) and how == "--root flag"
    # 2. env beats pin
    root, how = cfg.resolve_root(None)
    assert root == str(b.resolve()) and how == "CHIPCHAMP_ROOT"
    # 3. pin beats auto-detect
    monkeypatch.delenv("CHIPCHAMP_ROOT")
    root, how = cfg.resolve_root(None)
    assert root == str(c.resolve()) and how == "pinned default"


def test_resolve_autodetect_when_nothing_set(isolated_pin, tmp_path, monkeypatch):
    proj = tmp_path / "chip"
    (proj / ".chipchamp").mkdir(parents=True)
    monkeypatch.chdir(proj)
    root, how = cfg.resolve_root(None)
    assert root == str(proj.resolve())
    assert "project root" in how


def test_pin_and_unpin(isolated_pin, tmp_path):
    target = tmp_path / "work"; target.mkdir()
    assert cfg.pinned_root() is None
    cfg.pin_root(str(target))
    assert cfg.pinned_root() == str(target.resolve())
    assert cfg.unpin_root() is True
    assert cfg.pinned_root() is None
    assert cfg.unpin_root() is False  # idempotent


def test_inferred_target_named_after_repo(tmp_path):
    from chipchamp.config import Workspace
    repo = tmp_path / "myblock"
    (repo / "rtl").mkdir(parents=True)
    (repo / "rtl" / "foo.sv").write_text("module foo; endmodule\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.sv").write_text("module junk; endmodule\n")
    ws = Workspace(str(repo))
    assert ws.default_target == "myblock"           # named after the repo dir
    srcs = ws.target().sources
    assert any(s.endswith("rtl/foo.sv") for s in srcs)
    assert not any("node_modules" in s for s in srcs)  # vendor dirs skipped
