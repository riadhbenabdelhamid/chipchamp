"""Session store (SPEC §7.1, NFR-08): resumable, crash-safe conversations.

Persists the message history + task frame so a long task survives detach/restart
with ≤1 tool-call of lost work. Pending job ids live in the message log (tool
results), so a resumed session re-attaches to them.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..util.ids import new_session_id
from ..util.jsonio import atomic_write, dump_json, load_json


class Session:
    def __init__(self, session_id: str, store_dir: str, target: str = ""):
        self.id = session_id
        self.store_dir = Path(store_dir)
        self.target = target
        self.messages: list[dict] = []
        self.created = time.time()
        self.task_frame: dict = {}

    @classmethod
    def new(cls, store_dir: str, target: str = "") -> "Session":
        return cls(new_session_id(), store_dir, target)

    @classmethod
    def load(cls, session_id: str, store_dir: str) -> Optional["Session"]:
        p = Path(store_dir) / f"{session_id}.json"
        if not p.exists():
            return None
        data = load_json(p)
        s = cls(session_id, store_dir, data.get("target", ""))
        s.messages = data.get("messages", [])
        s.created = data.get("created", time.time())
        s.task_frame = data.get("task_frame", {})
        return s

    def append(self, role: str, content) -> None:
        self.messages.append({"role": role, "content": content})

    def save(self) -> None:
        atomic_write(self.store_dir / f"{self.id}.json", dump_json({
            "target": self.target, "created": self.created,
            "task_frame": self.task_frame, "messages": self.messages}))

    @staticmethod
    def _by_mtime(store_dir: str) -> list[tuple["Path", float]]:
        """(path, mtime) for every session, newest first — one stat per file.
        The single source of ordering for list_sessions / summaries / latest,
        so all three agree on which session is 'most recent'."""
        d = Path(store_dir)
        if not d.is_dir():
            return []
        pairs = []
        for p in d.glob("S-*.json"):
            try:
                pairs.append((p, p.stat().st_mtime))
            except OSError:
                continue
        pairs.sort(key=lambda t: t[1], reverse=True)
        return pairs

    @staticmethod
    def list_sessions(store_dir: str) -> list[str]:
        return [p.stem for p, _ in Session._by_mtime(store_dir)]

    @staticmethod
    def summaries(store_dir: str, limit: int = 20) -> list[dict]:
        """Newest-first session index for `/sessions`: id, target, when, size,
        and a one-line preview (first user message)."""
        out = []
        for p, mtime in Session._by_mtime(store_dir)[:limit]:
            try:
                data = load_json(p)
            except Exception:
                continue
            msgs = data.get("messages", [])
            first = next((m.get("content", "") for m in msgs
                          if m.get("role") == "user"
                          and isinstance(m.get("content"), str)), "")
            out.append({"id": p.stem, "target": data.get("target", ""),
                        "mtime": mtime, "messages": len(msgs),
                        "preview": " ".join(first.split())[:80]})
        return out

    @classmethod
    def latest(cls, store_dir: str) -> Optional["Session"]:
        """Most recently modified session, or None."""
        pairs = cls._by_mtime(store_dir)
        return cls.load(pairs[0][0].stem, store_dir) if pairs else None


def close_dangling_tool_calls(messages: list[dict],
                              note: str = "interrupted by user") -> int:
    """Give every unanswered assistant tool call a synthetic tool result.

    A Ctrl-C during a long tool call used to leave the transcript ending on
    an assistant turn whose tool_calls had no results. Both wire formats
    reject that (Anthropic: "tool_use ids without tool_result"; OpenAI: HTTP
    400, which the retry policy then retried three times), so every later
    call in that session failed — and so did resuming it. Returns how many
    tool turns were inserted; 0 means the transcript was already legal.
    """
    inserted = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if calls:
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            if not (nxt and nxt.get("role") == "tool"):
                messages.insert(i + 1, {"role": "tool", "content": [
                    {"id": tc.get("id", ""), "name": tc.get("name", ""),
                     "output": f'{{"error": "{note} — this call never ran"}}'}
                    for tc in calls]})
                inserted += 1
                i += 1
        i += 1
    return inserted
