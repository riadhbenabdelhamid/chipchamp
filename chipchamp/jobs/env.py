"""The environment a job step inherits.

EDA tools need a surprising amount of the caller's environment — license
servers (``*_LICENSE_FILE``), vendor roots (``XILINX_*``), module systems
(``MODULEPATH``), nix devshells (``NIX_*``), ``PYTHONPATH`` for cocotb — so a
narrow allowlist breaks real flows the first time it is honoured. What must
never reach a step is a credential: model-provider keys, cloud tokens, git
tokens. A farm submission (bsub/srun) forwards the environment to another
host, so the leak would not even stay on this machine.

Default: pass the environment through, minus anything that looks like a
secret. A workspace that wants hermetic steps sets ``[jobs] env_allowlist``
and gets strict allowlist semantics instead.
"""
from __future__ import annotations

import fnmatch
import os
from typing import Iterable, Mapping

# Names that look like credentials. Patterns are matched case-insensitively
# against the whole variable name. Kept deliberately narrow: `*_LICENSE_FILE`
# and `*_KEY_FILE`-style tool settings must survive.
SECRET_PATTERNS: tuple[str, ...] = (
    "*_API_KEY", "*_APIKEY", "*_SECRET", "*_SECRET_*", "*_TOKEN", "*_PASSWORD",
    "*_PASSWD", "*_CREDENTIALS", "AWS_ACCESS_KEY_ID", "ANTHROPIC_*", "OPENAI_*",
)

# What every step gets in strict (allowlist) mode on top of the workspace's
# own list — a shell cannot run without these.
BASE_ALLOW: tuple[str, ...] = ("PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG",
                               "LC_ALL", "TERM")


def looks_secret(name: str, extra: Iterable[str] = ()) -> bool:
    up = name.upper()
    return any(fnmatch.fnmatchcase(up, p.upper())
               for p in (*SECRET_PATTERNS, *extra))


def scrub_env(environ: Mapping[str, str] | None = None, *,
              allowlist: Iterable[str] | None = None,
              deny: Iterable[str] = ()) -> dict[str, str]:
    """Build a step's environment from `environ` (default: os.environ).

    `allowlist` non-empty → strict: only BASE_ALLOW plus these names (globs
    allowed) pass. Otherwise everything passes except names matching
    SECRET_PATTERNS or `deny`. Secrets are dropped in both modes — an
    allowlist entry cannot smuggle one through.
    """
    src = os.environ if environ is None else environ
    allow = [a.upper() for a in (allowlist or ())]
    out: dict[str, str] = {}
    for k, v in src.items():
        if looks_secret(k, deny):
            continue
        if allow:
            up = k.upper()
            if not (up in BASE_ALLOW
                    or any(fnmatch.fnmatchcase(up, a) for a in allow)):
                continue
        out[k] = v
    return out
