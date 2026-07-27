"""Shared test fixtures. Ensures the OSS EDA toolchain is on PATH so the
adapter/tool tests can drive real Verilator/Icarus/Yosys where installed; tests
that need a specific tool skip cleanly when it is absent."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "soc"

# keep the suite hermetic: semantic skill triggering would hit a live local
# embedding endpoint from any agent-loop test — pin the static index here
# (test_skills_semantic.py exercises semantic mode with a fake embedder)
os.environ.setdefault("CHIPCHAMP_SKILLS_INDEX", "compact")

# make common OSS toolchains discoverable without an activated shell
for cand in ("/home/riadh/oss-cad-suite/bin", "/home/riadh/verible/bin"):
    if os.path.isdir(cand) and cand not in os.environ.get("PATH", ""):
        os.environ["PATH"] = cand + os.pathsep + os.environ["PATH"]


def has(tool: str) -> bool:
    return shutil.which(tool) is not None


requires_icarus = pytest.mark.skipif(not has("iverilog"), reason="iverilog not installed")
requires_verilator = pytest.mark.skipif(not has("verilator"), reason="verilator not installed")
requires_yosys = pytest.mark.skipif(not has("yosys"), reason="yosys not installed")


@pytest.fixture
def example_root() -> str:
    return str(EXAMPLE)


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
