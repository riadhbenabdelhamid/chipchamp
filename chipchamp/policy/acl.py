"""Context ACLs (SPEC §13.2, FR-SEC-02).

Enforced in the tool layer, *below* the model: a jailbroken prompt cannot read a
denied path because the read never reaches the model — the tool refuses first.
Default-deny presets cover PDK/foundry/vendor-encrypted trees and license files.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

DEFAULT_DENY = [
    "**/pdk/**", "pdk/**", "**/foundry/**", "foundry/**",
    "**/*tsmc*/**", "**/*gf*/**/*.lib", "**/vendor_encrypted/**",
    "**/*.lic", "**/*.pdk", "**/*.cdl", "**/*.gds", "**/*.oas",
]


@dataclass
class ContextACL:
    deny: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)  # allow overrides deny
    use_defaults: bool = True

    def _deny_globs(self) -> list[str]:
        return (DEFAULT_DENY if self.use_defaults else []) + list(self.deny)

    def is_allowed(self, path: str) -> bool:
        norm = path.replace("\\", "/")
        for g in self.allow:
            if fnmatch.fnmatch(norm, g):
                return True
        for g in self._deny_globs():
            if fnmatch.fnmatch(norm, g) or fnmatch.fnmatch("/" + norm, g):
                return False
        return True

    def reason(self, path: str) -> str:
        norm = path.replace("\\", "/")
        for g in self._deny_globs():
            if fnmatch.fnmatch(norm, g) or fnmatch.fnmatch("/" + norm, g):
                return f"path matches deny rule '{g}' (IP/PDK protection, SPEC §13.2)"
        return "allowed"
