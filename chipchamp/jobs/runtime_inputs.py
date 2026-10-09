"""Validate the embedded Python observer's evidence before enabling resume."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .env import scrub_env
from .python_trace import digest


def python_environment(plan):
    spec = plan.meta["python_trace"]
    env = scrub_env({**scrub_env(), **plan.steps[0].env})
    try:
        result = subprocess.run(spec["probe"], cwd=plan.workdir, env=env,
                                capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        # Both the HDL compiler and runtime affect a cocotb verdict. Other
        # simulator backends can run, but have no complete dependency contract.
        binaries = {"icarus": ("iverilog", "vvp"), "verilator": ("verilator", "verilator_bin", "g++", "make")}
        data["hdl_tools"] = {name: digest(shutil.which(name, path=env.get("PATH")) or "")
                             for name in binaries.get(spec["hdl_sim"], ())}
        data["complete"] = bool(data.get("complete") and data["hdl_tools"]
                                and all(len(value) == 64 for value in data["hdl_tools"].values()))
        return data
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"complete": False}


def runtime_current(snapshot):
    for path, checksum in snapshot.get("runtime_files", {}).items():
        if digest(path) != checksum:
            return False
    for path, names in snapshot.get("runtime_directories", {}).items():
        try:
            current = sorted(os.listdir(path))
        except OSError:
            current = None
        if current != names:
            return False
    return True


def attach_runtime(plan, before, after):
    try:
        trace = json.loads(Path(plan.meta["python_trace"]["path"]).read_text())
        if (trace.get("schema_version") != 1 or not trace.get("finished")
                or not isinstance(trace.get("files"), dict)
                or not isinstance(trace.get("directories"), dict)):
            raise ValueError("invalid Python dependency trace")
        before["runtime_files"] = trace["files"]
        before["runtime_directories"] = trace["directories"]
        stable = (runtime_current(before) and
                  before["python_environment"] == after["python_environment"] and
                  all(before["files"].get(p, h) == h for p, h in trace["files"].items()))
        before["unstable"] |= not stable
        before["files"].update(trace["files"])
        before["complete"] = bool(before["python_static_complete"] and trace.get("complete")
                                  and not before["unstable"])
        before["reasons"].extend(trace.get("reasons", []))
    except (OSError, ValueError, TypeError):
        before["complete"] = False
        before["reasons"].append("Python runtime dependency trace is missing or invalid")
