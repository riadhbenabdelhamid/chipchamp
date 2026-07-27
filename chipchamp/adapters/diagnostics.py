"""Regex normalizers turning raw tool output into NormalizedDiagnostic records.

Keeping the messy per-tool parsing here lets the adapters stay short and makes
the normalization independently testable (SPEC §8.4).
"""
from __future__ import annotations

import re

from .base import NormalizedDiagnostic

# verible-verilog-lint:  path:line:col: message [rule-id]
_VERIBLE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)(?:-\d+)?:\s*(?P<msg>.*?)\s*\[(?P<code>[^\]]+)\]\s*$")

# verilator:  %Warning-NAME: file:line:col: msg   /  %Error: file:line:col: msg
_VERILATOR = re.compile(
    r"^%(?P<sev>Warning|Error)(?:-(?P<code>[\w-]+))?:\s*"
    r"(?:(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?)?\s*(?P<msg>.*)$")

# icarus/ghdl:  file:line: error|warning: msg
_ICARUS = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>error|warning):\s*(?P<msg>.*)$")

# yosys:  ERROR: ...   /   Warning: ...
_YOSYS = re.compile(r"^(?P<sev>ERROR|Warning):\s*(?P<msg>.*)$")


def parse_verible(text: str) -> list[NormalizedDiagnostic]:
    out = []
    for ln in text.splitlines():
        m = _VERIBLE.match(ln.strip())
        if m:
            out.append(NormalizedDiagnostic(
                tool="verible", severity="warning", category="lint",
                code=m["code"], file=m["file"], line=int(m["line"]),
                col=int(m["col"]), message=m["msg"]))
    return out


def parse_verilator(text: str, category: str = "lint") -> list[NormalizedDiagnostic]:
    out = []
    for ln in text.splitlines():
        m = _VERILATOR.match(ln)
        if not m:
            continue
        msg = (m["msg"] or "").strip()
        # drop the summary pseudo-error verilator prints when -Wall promotes
        # warnings to a fatal exit; the real warnings are already captured.
        if re.match(r"Exiting due to \d+ warning", msg):
            continue
        sev = "error" if m["sev"] == "Error" else "warning"
        out.append(NormalizedDiagnostic(
            tool="verilator", severity=sev,
            category="elab" if sev == "error" and not m["code"] else category,
            code=m["code"] or ("ELAB" if sev == "error" else "LINT"),
            file=m["file"] or "", line=int(m["line"]) if m["line"] else 0,
            col=int(m["col"]) if m["col"] else 0, message=(m["msg"] or "").strip()))
    return out


def parse_icarus(text: str) -> list[NormalizedDiagnostic]:
    out = []
    for ln in text.splitlines():
        m = _ICARUS.match(ln)
        if m:
            out.append(NormalizedDiagnostic(
                tool="iverilog", severity=m["sev"], category="elab",
                code="IVL", file=m["file"], line=int(m["line"]),
                col=int(m["col"]) if m["col"] else 0, message=m["msg"]))
    return out


def parse_yosys(text: str) -> list[NormalizedDiagnostic]:
    out = []
    for ln in text.splitlines():
        m = _YOSYS.match(ln.strip())
        if m:
            sev = "error" if m["sev"] == "ERROR" else "warning"
            code = "SYNTH"
            msg = m["msg"]
            if re.search(r"latch|proc_dlatch|inferring", msg, re.I):
                code = "INFERRED-LATCH"
            out.append(NormalizedDiagnostic(
                tool="yosys", severity=sev, category="synth", code=code,
                message=msg))
    return out
