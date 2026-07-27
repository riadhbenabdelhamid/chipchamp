"""RISC-V architectural compliance via RISCOF.

Co-simulation shows a core agrees with a model on *one program*; the
architectural test suite is what turns that into a conformance claim. RISCOF
(the official framework) runs `riscv-arch-test` on the DUT and on a reference
model and compares **signatures** — memory regions each test writes — so the
verdict never depends on a trace format.

chipchamp drives RISCOF as an external tool (like yosys or vivado) rather than
importing it: `riscv-config` hard-pins `pyyaml==5.2`, which must not be forced
on the host environment. The per-test verdict is read from RISCOF's own log
line — ``<name> : <commit id> : Passed|Failed``, emitted by
``riscof/framework/test.py`` — which is stabler than scraping its HTML report.
"""
from __future__ import annotations

import os
import re
import shutil

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# `<test name> : <commit id> : Passed`  (RISCOF formats these columns itself)
_RESULT = re.compile(r"^(?P<name>\S+)\s*:\s*(?P<commit>\S*)\s*:\s*"
                     r"(?P<status>Passed|Failed)\s*$")
_COUNT = re.compile(r"Following\s+(\d+)\s+tests have been run")


def find_riscof() -> str:
    """The riscof executable, or '' — expected on PATH, typically installed in
    its own venv precisely because of the pyyaml pin."""
    return shutil.which("riscof") or ""


def parse_riscof_log(text: str) -> dict:
    """{total, passed, failed, failures, tests} from a `riscof run` log."""
    tests: list[dict] = []
    reported_total = None
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        m = _COUNT.search(line)
        if m:
            reported_total = int(m.group(1))
        # strip colorlog's "INFO | " / "ERROR | " prefix before matching
        if "|" in line:
            line = line.split("|", 1)[1].strip()
        r = _RESULT.match(line)
        if r:
            tests.append({"name": r.group("name"),
                          "commit": r.group("commit"),
                          "status": r.group("status")})
    failures = [t["name"] for t in tests if t["status"] != "Passed"]
    return {"total": len(tests), "passed": len(tests) - len(failures),
            "failed": len(failures), "failures": failures, "tests": tests,
            "reported_total": reported_total}


def riscof_argv(riscof: str, *, suite: str, env: str, config: str = "",
                work_dir: str = "", testfile: str = "",
                no_ref_run: bool = False) -> list[str]:
    argv = [riscof, "run", "--suite", suite, "--env", env, "--no-browser"]
    if config:
        argv += ["--config", config]
    if work_dir:
        argv += ["--work-dir", work_dir]
    if testfile:
        argv += ["--testfile", testfile]
    if no_ref_run:
        argv.append("--no-ref-run")
    return argv


def guess_suite(root: str) -> tuple[str, str]:
    """(suite, env) inside a riscv-arch-test checkout, or ('','').

    The suite layout is `<root>/riscv-test-suite/` with `env/` beside it.
    """
    for base in (root, os.path.join(root, "riscv-arch-test")):
        suite = os.path.join(base, "riscv-test-suite")
        env = os.path.join(suite, "env")
        if os.path.isdir(suite) and os.path.isdir(env):
            return suite, env
    return "", ""
