"""Shared test fixtures. Ensures the OSS EDA toolchain is on PATH so the
adapter/tool tests can drive real Verilator/Icarus/Yosys where installed; tests
that need a specific tool skip cleanly when it is absent."""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# The tracked example workspace in the repo. Tests must never write into it:
# the `ctx`/`workspace` fixtures used to point here, and 131 tests left stub
# RTL under work/, rewrote tracked files in place and piled up thousands of
# job records in .chipchamp/runs. Use REAL_EXAMPLE only to READ artifacts
# that live there (a kept physical run); everything else gets a copy.
REAL_EXAMPLE = REPO / "examples" / "soc"

# Dirs under the example that are generated, machine-written state — never
# part of a pristine copy (mirrors .gitignore for a checkout without git).
_SKIP_DIRS = {".chipchamp/runs", ".chipchamp/sessions", ".chipchamp/bundles",
              ".chipchamp/experiments", ".chipchamp/fpga", ".chipchamp/ppa",
              ".chipchamp/sweep", ".chipchamp/riscv", ".chipchamp/lec",
              ".chipchamp/mcp-images", ".chipchamp/power", ".chipchamp/glsim",
              ".chipchamp/formal", ".chipchamp/coverage", "work", "obj_dir"}


def copy_example(dst: Path) -> Path:
    """A pristine copy of the example workspace: exactly the tracked files
    (git ls-files), or — without git — the tree minus generated state."""
    dst = Path(dst)
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z",
                              "examples/soc"], capture_output=True, check=True)
        rels = [r for r in out.stdout.decode().split("\0") if r]
        files = [(REPO / r, dst / Path(r).relative_to("examples/soc")) for r in rels]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for src in REAL_EXAMPLE.rglob("*"):
            rel = src.relative_to(REAL_EXAMPLE)
            if not src.is_file() or any(str(rel).startswith(d) for d in _SKIP_DIRS):
                continue
            if src.suffix in (".vcd", ".vvp", ".pyc") or "db-" in src.name:
                continue
            files.append((src, dst / rel))
    for src, to in files:
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, to)
    return dst


# One session-wide copy for the tests that need a path at import time (the
# REPL subprocess tests, the bench harness). Removed when the interpreter
# exits. Per-test isolation is the `example_root` fixture below.
_SESSION_COPY = Path(tempfile.mkdtemp(prefix="chipchamp-example-"))
EXAMPLE = copy_example(_SESSION_COPY / "soc")
atexit.register(shutil.rmtree, _SESSION_COPY, True)

# keep the suite hermetic: semantic skill triggering would hit a live local
# embedding endpoint from any agent-loop test — pin the static index here
# (test_skills_semantic.py exercises semantic mode with a fake embedder)
os.environ.setdefault("CHIPCHAMP_SKILLS_INDEX", "compact")

# make common OSS toolchains discoverable without an activated shell
for cand in (os.path.expanduser("~/oss-cad-suite/bin"),
             os.path.expanduser("~/verible/bin")):
    if os.path.isdir(cand) and cand not in os.environ.get("PATH", ""):
        os.environ["PATH"] = cand + os.pathsep + os.environ["PATH"]


def has(tool: str) -> bool:
    return shutil.which(tool) is not None


requires_icarus = pytest.mark.skipif(not has("iverilog"), reason="iverilog not installed")
requires_verilator = pytest.mark.skipif(not has("verilator"), reason="verilator not installed")
requires_yosys = pytest.mark.skipif(not has("yosys"), reason="yosys not installed")


@pytest.fixture
def example_root(tmp_path) -> str:
    """A fresh copy of the tracked example workspace for THIS test. Writes,
    rebuilt indexes and job records land in tmp_path, never in the repo."""
    return str(copy_example(tmp_path / "soc"))


@pytest.fixture
def workspace(example_root):
    from chipchamp.config import Workspace
    ws = Workspace(example_root)
    # rebuild the index fresh each session
    ws.db(rebuild=True)
    return ws


@pytest.fixture
def ctx(workspace):
    from chipchamp.tools.context import ToolContext
    return ToolContext(workspace)
