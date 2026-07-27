"""Anti-gaming detectors (SPEC §11.4, FR-GAME-01/02).

Assumes a well-meaning but reward-sensitive agent and closes the obvious
exploits: making the gates green by deleting tests, disabling assertions,
shrinking timeouts, narrowing randomization, pinning seeds, or editing coverage
definitions. Each finding blocks ``report.done`` (the ``no_gaming`` gate) until a
human acknowledges it — the agent cannot self-acknowledge.
"""
from __future__ import annotations

import re

from .diff import Diff

_ASSERT_RE = re.compile(r"\b(assert|assume|cover)\s+(property|final|\()", re.I)
from ..brand import markers as _markers
_CHECK_RE = re.compile(
    r"\$(error|fatal|assert)|" + "|".join(_markers("FAIL")) + r"|!==|mismatch",
    re.I)
_RAND_RE = re.compile(r"\b(rand|randc)\b")
_COVER_RE = re.compile(r"\b(covergroup|coverpoint|bins|cross|ignore_bins|illegal_bins)\b")
_TIMEOUT_RE = re.compile(r"(?:watchdog|timeout|#)\s*[:=(]?\s*(\d{3,})")


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _count(rx: re.Pattern, text: str) -> int:
    return sum(1 for ln in text.splitlines()
              if rx.search(ln) and not ln.strip().startswith("//"))


def _max_timeout(text: str) -> int:
    vals = [int(m.group(1)) for m in _TIMEOUT_RE.finditer(text)]
    return max(vals) if vals else 0


def scan(diff: Diff, closure_claim: bool = False) -> list[dict]:
    findings: list[dict] = []

    def add(kind: str, path: str, detail: str):
        findings.append({"kind": kind, "file": path, "detail": detail,
                         "acknowledged": False})

    # deleted test/verification files
    for f in diff.files:
        if f.status == "deleted" and (f.is_tb or "test" in f.path.lower()):
            add("removed_test_file", f.path, "verification file deleted")

    for f in diff.files:
        if f.status == "deleted":
            continue
        old, new = f.old_text, f.new_text

        # assertions/checks weakened
        if _count(_ASSERT_RE, old) > _count(_ASSERT_RE, new):
            add("disabled_assertion", f.path,
                f"SVA count dropped {_count(_ASSERT_RE, old)}→{_count(_ASSERT_RE, new)}")
        if _count(_CHECK_RE, old) > _count(_CHECK_RE, new):
            add("weakened_check", f.path,
                f"self-check count dropped {_count(_CHECK_RE, old)}→{_count(_CHECK_RE, new)}")

        # timeout shrink
        ot, nt = _max_timeout(old), _max_timeout(new)
        if ot and nt and nt < ot // 2:
            add("reduced_timeout", f.path, f"watchdog/timeout cut {ot}→{nt}")

        # randomization narrowed / removed
        if _count(_RAND_RE, old) > _count(_RAND_RE, new):
            add("narrowed_randomization", f.path,
                f"rand/randc count dropped {_count(_RAND_RE, old)}→{_count(_RAND_RE, new)}")

        # newly pinned seed in a previously non-pinned test
        if ("+seed=" in new or re.search(r"srandom\(\s*\d+\s*\)", new)) and \
           ("+seed=" not in old and not re.search(r"srandom\(\s*\d+\s*\)", old)):
            add("pinned_seed", f.path, "a fixed seed was introduced")

        # coverage definition edits (FR-GAME-02)
        if _COVER_RE.search(old) or _COVER_RE.search(new):
            if old != new:
                add("covergroup_edit", f.path,
                    "coverage definition changed"
                    + (" alongside a closure claim" if closure_claim else ""))

    return findings
