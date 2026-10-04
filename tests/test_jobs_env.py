"""Job steps inherit the environment minus credentials; a workspace can opt
into a strict allowlist. (The old allowlist was dead code — `or True` — so
every step, including farm submissions, received the model API keys.)"""
from __future__ import annotations

from chipchamp.jobs.env import BASE_ALLOW, scrub_env

ENV = {
    "PATH": "/usr/bin", "HOME": "/h", "USER": "u",
    "LM_LICENSE_FILE": "27000@lic", "XILINXD_LICENSE_FILE": "x",
    "MODULEPATH": "/mods", "NIX_PATH": "nixpkgs=/n", "PYTHONPATH": "/py",
    "ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oa",
    "CHIPCHAMP_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "aws",
    "AWS_ACCESS_KEY_ID": "AKIA", "GITHUB_TOKEN": "gh", "DB_PASSWORD": "pw",
    "GOOGLE_APPLICATION_CREDENTIALS": "/c.json",
}


def test_default_passes_tool_settings_and_drops_secrets():
    env = scrub_env(ENV)
    for keep in ("PATH", "LM_LICENSE_FILE", "XILINXD_LICENSE_FILE",
                 "MODULEPATH", "NIX_PATH", "PYTHONPATH"):
        assert keep in env
    for gone in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CHIPCHAMP_API_KEY",
                 "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN",
                 "DB_PASSWORD", "GOOGLE_APPLICATION_CREDENTIALS"):
        assert gone not in env


def test_extra_deny_patterns_from_config():
    env = scrub_env(ENV, deny=["LM_*"])
    assert "LM_LICENSE_FILE" not in env and "PATH" in env


def test_strict_allowlist_is_strict_but_keeps_the_shell_basics():
    env = scrub_env(ENV, allowlist=["*_LICENSE_FILE"])
    assert set(env) == {k for k in ENV if k in BASE_ALLOW} | {
        "LM_LICENSE_FILE", "XILINXD_LICENSE_FILE"}


def test_allowlist_cannot_smuggle_a_secret():
    env = scrub_env(ENV, allowlist=["ANTHROPIC_API_KEY", "PATH"])
    assert "ANTHROPIC_API_KEY" not in env and "PATH" in env


def test_runner_uses_the_scrubbed_env(tmp_path, monkeypatch):
    """End to end through JobRunner._run_step: the child must not see the key."""
    import sys
    from chipchamp.adapters.base import Step
    from chipchamp.jobs.runner import JobRunner
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("LM_LICENSE_FILE", "27000@lic")
    r = JobRunner(str(tmp_path / "runs"), registry=None)
    st = Step(argv=[sys.executable, "-c",
                    "import os,sys; sys.stdout.write(os.environ.get('ANTHROPIC_API_KEY','ABSENT')"
                    "+'|'+os.environ.get('LM_LICENSE_FILE','ABSENT'))"],
              cwd=str(tmp_path))
    res = r._run_step(st, tmp_path, 0, timeout=30)
    assert res.stdout.strip() == "ABSENT|27000@lic"
