"""The project notebook: what this workspace learned, across sessions.

Sessions are transcripts and the design DB is rebuilt from source, so session
#40 knew nothing session #3 learned. Everything hard-won — why that CDC path is
benign, which oracle was wrong rather than the RTL, what actually fixed the
overflow — died with the conversation that discovered it.

This is the store for the small set of things that are *not* derivable from the
tree: a root cause and its mechanism, a fix pattern that worked, a constraint
the hardware imposes. Not a log. The test is whether a competent engineer
joining tomorrow would want it written on the whiteboard.

Deliberately narrow, for two reasons. A digest of it goes into the system
prompt beside DESIGN.md, so every entry is paid for on every request — which is
exactly the pressure that keeps it honest. And an agent that recorded
everything would produce a file nobody reads, which is the same as no file at
all.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .util.ids import deterministic_id
from .util.jsonio import atomic_write, dump_json, load_json

# what a note can be about. Each is something the tree cannot tell you.
KINDS = ("root_cause", "fix_pattern", "constraint", "decision", "gotcha")

# entries in the prompt digest — a hard cap, because the digest is rent paid on
# every single request
DIGEST_N = 12


class Notebook:
    """Durable findings in ``.chipchamp/notebook.json``, newest last."""

    def __init__(self, dot: str | Path, limit: int = 200):
        self.path = Path(dot) / "notebook.json"
        self.limit = limit
        self._data: Optional[list] = None

    def _load(self) -> list:
        if self._data is None:
            try:
                d = load_json(self.path)
                self._data = d if isinstance(d, list) else []
            except Exception:
                self._data = []
        return self._data

    def add(self, kind: str, subject: str, detail: str, *,
            evidence: str = "", source: str = "") -> dict:
        """Record one finding. Returns it, or an error dict.

        `evidence` is a job id or file:line. A finding with no evidence is
        still worth keeping — some of the best ones come from a human — but the
        field exists to make the difference visible rather than to let prose
        and measurement blur together."""
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            return {"error": f"kind must be one of {', '.join(KINDS)}"}
        subject = " ".join((subject or "").split())[:120]
        detail = (detail or "").strip()[:900]
        if not subject or not detail:
            return {"error": "a note needs both a subject and a detail"}
        entries = self._load()
        note = {"id": deterministic_id("N", kind, subject, f"{time.time():.6f}"),
                "kind": kind, "subject": subject, "detail": detail,
                "evidence": evidence[:120], "source": source[:80],
                "ts": round(time.time(), 1)}
        # Same subject and kind → an update, not a second entry. Otherwise a
        # recurring investigation would fill the digest with its own echoes.
        for i, e in enumerate(entries):
            if e.get("kind") == kind and e.get("subject") == subject:
                note["id"] = e["id"]
                note["supersedes"] = e.get("ts")
                entries[i] = note
                self.save()
                return note
        entries.append(note)
        del entries[:-self.limit]
        self.save()
        return note

    def remove(self, note_id: str) -> bool:
        """A finding that turns out to be wrong is worse than none — it has to
        be removable, and by the same id the digest shows."""
        entries = self._load()
        keep = [e for e in entries if e.get("id") != note_id]
        if len(keep) == len(entries):
            return False
        self._data = keep
        self.save()
        return True

    def save(self) -> None:
        try:
            atomic_write(self.path, dump_json(self._load()))
        except OSError:
            pass

    def entries(self, kind: str = "") -> list[dict]:
        e = list(reversed(self._load()))
        return [x for x in e if x.get("kind") == kind] if kind else e

    def digest(self, limit: int = DIGEST_N) -> str:
        """The prompt-side view: newest findings, one line each.

        Presented as what the project knows, not as instructions — a note is
        evidence from the past, and the model must be free to find it wrong."""
        rows = self.entries()[:limit]
        if not rows:
            return ""
        lines = ["## What this project has already learned",
                 "Findings recorded by earlier sessions. Treat them as prior "
                 "evidence, not as orders — if the design has moved on, say so.",
                 ""]
        for e in rows:
            ev = f" [{e['evidence']}]" if e.get("evidence") else ""
            lines.append(f"- ({e['kind']}) {e['subject']}: {e['detail'][:200]}{ev}")
        return "\n".join(lines)
