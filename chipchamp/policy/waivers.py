"""Waivers as data (SPEC §12.6, FR-ADPT-03, §11.4).

A waiver suppresses a diagnostic *by record*, not by edit: entries live in
``waivers/*.yaml`` (a protected path the agent cannot write), carry a
justification, an author, an approver and an expiry, and only ``status: active``
entries with an approver take effect. The agent may *draft* waivers — drafts go
to ``.chipchamp/proposed_waivers.yaml`` (agent-writable staging); a human reviews
and moves them into ``waivers/`` to activate.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class Waiver:
    rule: str            # normalized diagnostic code (glob ok, e.g. "UNUSED*")
    scope: str           # file glob
    justification: str = ""
    author: str = ""
    approver: Optional[str] = None
    status: str = "proposed"  # proposed | active
    expires: str = ""         # YYYY-MM-DD ("" = never)

    @property
    def id(self) -> str:
        return f"{self.rule}@{self.scope}"

    def is_active(self) -> bool:
        if self.status != "active" or not self.approver:
            return False
        if self.expires:
            try:
                expiry = time.strptime(self.expires, "%Y-%m-%d")
                if time.mktime(expiry) < time.time():
                    return False
            except ValueError:
                return False  # unparseable expiry = not active (conservative)
        return True

    def matches(self, code: str, path: str) -> bool:
        norm = path.replace("\\", "/")
        return fnmatch.fnmatch(code, self.rule) and (
            fnmatch.fnmatch(norm, self.scope)
            or fnmatch.fnmatch(os.path.basename(norm), self.scope)
            or norm.endswith(self.scope))


def load_waivers(root: str) -> list[Waiver]:
    out: list[Waiver] = []
    for f in sorted(glob.glob(os.path.join(root, "waivers", "*.yaml"))):
        try:
            entries = yaml.safe_load(open(f)) or []
        except yaml.YAMLError:
            continue
        for e in entries if isinstance(entries, list) else []:
            out.append(Waiver(
                rule=str(e.get("rule", "")), scope=str(e.get("scope", "")),
                justification=e.get("justification", ""),
                author=e.get("author", ""), approver=e.get("approver"),
                status=e.get("status", "proposed"),
                expires=str(e.get("expires", "") or "")))
    return out


def apply_waivers(diagnostics: list, waivers: list[Waiver], root: str = "") -> dict:
    """Mark matching diagnostics with their ACTIVE waiver (FR-ADPT-03). Returns
    counts. Proposed/expired/unapproved waivers never suppress anything."""
    active = [w for w in waivers if w.is_active()]
    waived = 0
    for d in diagnostics:
        path = d.file
        if root and path.startswith(root):
            path = os.path.relpath(path, root)
        for w in active:
            if w.matches(d.code, path):
                d.waiver = w.id
                waived += 1
                break
    return {"active_waivers": len(active), "total_waivers": len(waivers),
            "waived_diagnostics": waived}


def draft_waiver(dot_dir: str, *, rule: str, scope: str, justification: str,
                 author: str = "chipchamp-agent") -> str:
    """Append a PROPOSED waiver to the agent-writable staging file. It has no
    effect until a human moves it into waivers/ with status: active + approver."""
    path = os.path.join(dot_dir, "proposed_waivers.yaml")
    entries = []
    if os.path.exists(path):
        entries = yaml.safe_load(open(path)) or []
    entries.append({"rule": rule, "scope": scope, "justification": justification,
                    "author": author, "approver": None, "status": "proposed"})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        yaml.safe_dump(entries, fh, sort_keys=False)
    return path
