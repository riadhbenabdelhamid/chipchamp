"""Native and embedded-Python dependency closure with real simulator runs."""
import json
import os
from pathlib import Path

import pytest
import yaml

from chipchamp.adapters.cocotb_adapter import CocotbAdapter
from chipchamp.adapters.verilator import VerilatorAdapter
from chipchamp.config import Workspace
from chipchamp.jobs.inputs import snapshot_current
from chipchamp.tools.context import ToolContext
from chipchamp.tools.verif_tools import regress_run
from test_verification_planning import design  # noqa: F401


def configure(design, runner):
    path = design.ws.dot / "tests.yaml"
    path.write_text(yaml.safe_dump({"tests": [{"name": "runtime", "target": "chip", "runner": runner}]}))
    return ToolContext(Workspace(design.ws.root))


def test_runtime_hashes_negative_inputs_and_never_read_special_files(tmp_path):
    from chipchamp.jobs.python_trace import digest
    from chipchamp.jobs.runtime_inputs import runtime_current
    path = tmp_path / "stimulus"
    old = {"runtime_files": {str(path): digest(path)}}
    assert runtime_current(old)
    path.write_text("new dependency")
    assert not runtime_current(old)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert digest(fifo) == "UNSUPPORTED"
    assert digest(os.devnull) == "UNSUPPORTED"
    other = tmp_path / "other"
    other.write_text("other bytes")
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    prior = {"runtime_files": {str(alias): digest(alias)}}
    alias.unlink()
    alias.symlink_to(other)
    assert not runtime_current(prior)


def test_live_cpp_resume_and_transitive_header_change(design):
    if not VerilatorAdapter().available():
        pytest.skip("Verilator unavailable")
    root = Path(design.ws.root)
    (root / "tb/value.hpp").write_text("constexpr int expected = 0;\n")
    (root / "tb/helper.hpp").write_text('#ifdef __OPTIMIZE__\n#include "value.hpp"\n#else\n#include "unoptimized.hpp"\n#endif\n')
    (root / "tb/main.cpp").write_text('''#include "Vwrapper.h"
#include "helper.hpp"
#include <iostream>
int main() { Vwrapper dut; dut.eval();
if (dut.q != expected) return 1;
std::cout << "CHIPCHAMP_PASS: cpp checked" << std::endl; return 0; }
''')
    ctx = configure(design, {"kind": "verilator_cpp", "top": "wrapper", "cpp": ["tb/main.cpp"],
                             "cpp_incdirs": ["tb"]})
    first = regress_run(ctx, tests=["runtime"], seeds=[1])
    assert first["passed"] == 1, first
    rec = ctx.task_jobs[-1]
    assert rec.execution_key, rec.input_snapshot["reasons"]
    assert str(root / "tb/value.hpp") in rec.input_snapshot["files"]
    assert snapshot_current(rec.input_snapshot)
    second = regress_run(ctx, resume=first["checkpoint"])
    assert second["resumed"] == 1, second
    (root / "tb/value.hpp").write_text("constexpr int expected = 0; // edited\n")
    assert not snapshot_current(rec.input_snapshot)
    third = regress_run(ctx, resume=first["checkpoint"])
    assert third["resumed"] == 0 and third["passed"] == 1, third


def test_live_cocotb_resume_helper_data_and_missing_trace(design, tmp_path, monkeypatch):
    if not CocotbAdapter().available():
        pytest.skip("cocotb unavailable")
    root = Path(design.ws.root)
    package = tmp_path / "packages"
    package.mkdir()
    external = package / "stimulus_helper.py"
    external.write_text("offset = 0\n")
    monkeypatch.setenv("PYTHONPATH", str(package))
    (root / "stimulus.txt").write_text("0")
    (root / "tb/helper.py").write_text('import importlib\ndef expected():\n    helper = importlib.import_module("stimulus_helper")\n    with open("../stimulus.txt") as stream:\n        return int(stream.read()) + helper.offset\n')
    (root / "tb/test_runtime.py").write_text('''import cocotb
from cocotb.triggers import Timer
from helper import expected
@cocotb.test()
async def checked(dut):
    await Timer(1, unit="ns")
    assert int(dut.q.value) == expected()
''')
    ctx = configure(design, {"kind": "cocotb", "module": "test_runtime", "toplevel": "wrapper",
                             "test_dir": "tb", "hdl_sim": "icarus"})
    first = regress_run(ctx, tests=["runtime"], seeds=[1])
    assert first["passed"] == 1, first
    rec = ctx.task_jobs[-1]
    trace = json.loads(Path(rec.artifacts["python_trace"]).read_text())
    assert rec.execution_key, {"snapshot": rec.input_snapshot["reasons"], "trace": trace["reasons"],
                               "static": rec.input_snapshot["python_static_complete"],
                               "unstable": rec.input_snapshot["unstable"]}
    assert str(root / "stimulus.txt") in rec.input_snapshot["runtime_files"]
    assert snapshot_current(rec.input_snapshot)
    second = regress_run(ToolContext(Workspace(ctx.ws.root)), resume=first["checkpoint"])
    assert second["resumed"] == 1, second
    assert str(external) in rec.input_snapshot["runtime_files"]
    external.write_text("offset = 0 # changed external dependency\n")
    assert not snapshot_current(rec.input_snapshot)
    external_changed = regress_run(ctx, resume=first["checkpoint"])
    assert external_changed["resumed"] == 0 and external_changed["passed"] == 1, external_changed
    (root / "stimulus.txt").write_text("00")
    assert not snapshot_current(ctx.task_jobs[-1].input_snapshot)
    third = regress_run(ctx, resume=first["checkpoint"])
    assert third["resumed"] == 0 and third["passed"] == 1, third
    helper = root / "tb/helper.py"
    helper.write_text(helper.read_text() + "# changed\n")
    fourth = regress_run(ctx, resume=first["checkpoint"])
    assert fourth["resumed"] == 0 and fourth["passed"] == 1, fourth
    Path(ctx.task_jobs[-1].artifacts["python_trace"]).unlink()
    fifth = regress_run(ctx, resume=first["checkpoint"])
    assert fifth["resumed"] == 0 and fifth["passed"] == 1, fifth


@pytest.mark.parametrize("activity", [
    "from os.path import exists as present; assert present('../rtl/leaf.sv')",
    "from pathlib import Path; assert Path('../rtl/leaf.sv').exists()",
    "import subprocess; subprocess.run(['true'], check=True)",
    "import os; os.chdir('..')",
    "import threading; worker = threading.Thread(target=lambda: None); worker.start(); worker.join()",
])
def test_live_cocotb_untracked_activity_forces_rerun(design, activity):
    if not CocotbAdapter().available():
        pytest.skip("cocotb unavailable")
    root = Path(design.ws.root)
    (root / "tb/test_runtime.py").write_text('''import cocotb
from cocotb.triggers import Timer
@cocotb.test()
async def checked(dut):
    await Timer(1, unit="ns")
    ''' + activity + '\n')
    ctx = configure(design, {"kind": "cocotb", "module": "test_runtime", "toplevel": "wrapper",
                             "test_dir": "tb", "hdl_sim": "icarus"})
    first = regress_run(ctx, tests=["runtime"], seeds=[1])
    assert first["passed"] == 1, first
    rec = ctx.task_jobs[-1]
    assert not rec.execution_key and not rec.input_snapshot["complete"]
    assert any("untraced" in r for r in rec.input_snapshot["reasons"]), rec.input_snapshot["reasons"]
    again = regress_run(ctx, resume=first["checkpoint"])
    assert again["resumed"] == 0 and again["passed"] == 1, again
