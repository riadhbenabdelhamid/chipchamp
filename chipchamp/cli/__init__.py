"""The chipchamp CLI package. `cli` is the click root; `main` the entry point.

Commands live by subject — design, physical, riscv, verify, model, repl,
knowledge — and register onto the root when their module is imported here.
Every name those modules define is re-exported, so `chipchamp.cli.<name>`
keeps resolving exactly as it did when the CLI was one file (the test
suite reaches for private helpers and command objects by that path).
"""
from __future__ import annotations

import json  # noqa: F401  (tests patch cli.sys / read cli.json)
import os  # noqa: F401
import sys  # noqa: F401

from ._core import cli, main  # noqa: F401
from . import _core, design, physical, riscv, verify, model, repl, knowledge  # noqa: E402

for _mod in (_core, design, physical, riscv, verify, model, repl, knowledge):
    for _name, _value in vars(_mod).items():
        if not _name.startswith("__") and _name not in globals():
            globals()[_name] = _value
del _mod, _name, _value
