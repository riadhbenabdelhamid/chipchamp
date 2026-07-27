"""A minimal change-set model the policy engine reasons over (SPEC §11).

Chipchamp is VCS-abstracted (git in M0, Perforce in M2). ``Diff`` captures just
what classification, gating and anti-gaming need: per-file status and old/new
text. It can be built from a git worktree or synthesized by the agent's own edits.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileChange:
    path: str
    status: str  # added | modified | deleted
    old_text: str = ""
    new_text: str = ""

    @property
    def is_rtl(self) -> bool:
        return self.path.endswith((".sv", ".v", ".svh", ".vh", ".vhd", ".vhdl")) \
            and "/tb" not in self.path and "/test" not in self.path \
            and not Path(self.path).name.startswith("tb_")

    @property
    def is_tb(self) -> bool:
        p = self.path
        return ("/tb/" in p or "/test" in p or Path(p).name.startswith("tb_")
                or p.endswith((".py",)) and "cocotb" in p) and \
            p.endswith((".sv", ".v", ".svh", ".py"))

    @property
    def is_doc(self) -> bool:
        return self.path.endswith((".md", ".txt", ".rst")) or "/docs/" in self.path

    @property
    def is_regmap_source(self) -> bool:
        p = Path(self.path)
        if self.path.endswith((".rdl", ".hjson", ".ipxact")):
            return True
        if self.path.endswith((".json", ".yaml", ".yml")):
            # regs/timer.yaml, regmap/*.json, *_regs.yaml, reg_*.json …
            return any(part in ("regs", "regmap", "registers") for part in p.parts) \
                or "reg" in p.stem
        return False

    def in_globs(self, globs: list[str]) -> bool:
        import fnmatch
        return any(fnmatch.fnmatch(self.path, g) for g in globs)

    @property
    def is_build_script(self) -> bool:
        return self.path.endswith((".f", ".tcl", ".mk", ".make", ".cmake")) \
            or Path(self.path).name in ("Makefile", "makefile")


@dataclass
class Diff:
    files: list[FileChange] = field(default_factory=list)
    declared_nfc: bool = False  # agent asserts "no functional change"
    declared_cdc: bool = False
    declared_timing: bool = False

    def touched_rtl_modules(self) -> list[str]:
        return [f.path for f in self.files if f.is_rtl]

    @classmethod
    def from_git(cls, repo: str, ref: str = "HEAD") -> "Diff":
        d = cls()
        try:
            out = subprocess.run(["git", "-C", repo, "diff", "--name-status", ref],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return d
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, path = parts[0], parts[-1]
            status = {"A": "added", "M": "modified", "D": "deleted"}.get(code[0], "modified")
            new_text = ""
            if status != "deleted":
                fp = Path(repo) / path
                new_text = fp.read_text(errors="replace") if fp.exists() else ""
            old_text = _git_show(repo, ref, path) if status != "added" else ""
            d.files.append(FileChange(path=path, status=status,
                                      old_text=old_text, new_text=new_text))
        return d


def _git_show(repo: str, ref: str, path: str) -> str:
    try:
        out = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
