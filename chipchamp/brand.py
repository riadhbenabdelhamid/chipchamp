"""App branding — the product name lives HERE, once, as a variable.

Every user-facing surface (banner, CLI help, env vars, pass/fail markers, the
workspace dot-directory) derives from :data:`APP_NAME` at call time, so the
main program can rebrand the whole tool with one call before anything renders:

    from chipchamp import brand
    brand.set_name("myfab")        # banner, CHIPCHAMP→MYFAB_* env, markers …

`LEGACY_NAMES` is the seam for a future rename: env vars, testbench markers and
workspace dot-dirs written under an earlier name keep resolving because every
lookup tries the current name first and then each legacy one. It is empty today
— this repository has only ever shipped as chipchamp — but the plumbing stays,
because the cost of a rename is exactly the artifacts already on users' disks.
"""
from __future__ import annotations

import os

APP_NAME = "chipchamp"

_DEFAULT_NAME = "chipchamp"  # the shipped name — always an accepted era, so
#                              a set_name() rebrand can't orphan its artifacts

# Names this project shipped under before, newest first. Add to this on a
# rename and every prior artifact keeps resolving; a rename is a decision about
# the product's name, never a licence to orphan the jobs, sessions and config a
# user already has on disk.
LEGACY_NAMES: tuple[str, ...] = ()


def set_name(name: str) -> None:
    """Rebrand at runtime (call from the main program before rendering)."""
    global APP_NAME
    name = (name or "").strip()
    if name:
        APP_NAME = name


def all_names() -> tuple[str, ...]:
    """Current name first, then the shipped default, then legacy — the
    lookup/acceptance order everywhere."""
    out = [APP_NAME]
    for n in (_DEFAULT_NAME, *LEGACY_NAMES):
        if n not in out:
            out.append(n)
    return tuple(out)


_all_names = all_names  # internal alias


# ---- environment variables ---------------------------------------------------

def env_name(suffix: str) -> str:
    """The canonical env var for `suffix` under the current brand — use this
    when TELLING the user which variable to set (docs, error messages)."""
    return f"{APP_NAME.upper()}_{suffix}"


def env(suffix: str, default=None):
    """Read `<BRAND>_<suffix>`, falling back through every earlier era — so a
    variable exported under a previous name keeps being honoured, with the
    current name winning when both are set."""
    for name in _all_names():
        v = os.environ.get(f"{name.upper()}_{suffix}")
        if v is not None:
            return v
    return default


# ---- testbench pass/fail markers ---------------------------------------------

def marker(kind: str) -> str:
    """The marker a NEW harness should print (e.g. CHIPLAB_PASS)."""
    return f"{APP_NAME.upper()}_{kind}"


def markers(kind: str) -> tuple[str, ...]:
    """Every marker a parser should ACCEPT — current brand plus every earlier
    era, so a harness generated before a rename still verifies."""
    return tuple(f"{n.upper()}_{kind}" for n in _all_names())


# ---- workspace dot-directory -------------------------------------------------

def dot_dir_names() -> tuple[str, ...]:
    """Dot-dir candidates in lookup order: the current brand, then each
    earlier era."""
    return tuple(f".{n}" for n in _all_names())


def dot_dir(root) -> "os.PathLike":
    """The workspace dot-dir under `root`: an existing legacy dir keeps being
    used (a rename must not orphan a workspace's jobs/sessions/config); a
    fresh workspace gets the current brand's name.

    The workspace's own machinery mkdirs subpaths under the dot-dir (runs/,
    sessions/), so "current name exists" alone must NOT win — an accidentally
    spawned empty .chipchamp would silently shadow an earlier era's dir that
    holds the real config. The dir that contains config.toml is the workspace;
    name order only breaks ties."""
    from pathlib import Path
    root = Path(root)
    cands = [root / d for d in dot_dir_names()]
    for c in cands:
        if (c / "config.toml").is_file():
            return c
    for c in cands:
        if c.is_dir():
            return c
    return cands[0]
