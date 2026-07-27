"""Agent Skills (SPEC §15.3): user-authored expert playbooks the agent can
discover and load on demand.

A skill is a directory containing a ``SKILL.md`` in the Anthropic Agent Skills
format — YAML frontmatter with ``name`` and ``description`` (the description
carries the "Use when …" trigger prose), a markdown body, and optionally
bundled reference files (``references/…``) the body points at.

Discovery is layered, first-occurrence-wins (the packs idiom):

1. ``<root>/.chipchamp/skills/`` — implicit workspace skills
2. ``[skills] dirs = [...]`` in config.toml — versioned team intent
3. the CLI-managed registry (``.chipchamp/skills.json``) — ad-hoc pointers
   added with ``chipchamp skills add <dir>``

Skills are knowledge, not code: nothing here executes skill content. Bodies
enter the model's context only through ``skill.use``/``/skill`` — and pass
through the FR-SEC-05 injection scanner like any other untrusted text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .util.jsonio import atomic_write, dump_json, load_json

INDEX_MODES = ("semantic", "compact", "full", "off")
_INDEX_CAP = 16_000  # chars; keeps a huge corpus from flooding the prompt


@dataclass
class Skill:
    name: str
    description: str
    dir: Path
    source: str  # workspace | config | registry


@dataclass
class DiscoveryReport:
    skills: dict[str, Skill] = field(default_factory=dict)
    shadowed: list[str] = field(default_factory=list)  # "name (dir) by (dir)"
    warnings: list[str] = field(default_factory=list)


# ---- SKILL.md parsing --------------------------------------------------------


def parse_frontmatter(text: str) -> dict | None:
    """The YAML block between leading ``---`` fences, or None if absent or
    malformed. Never raises — discovery must survive a bad file.

    Falls back to a lenient line parser when strict YAML rejects the block:
    real-world skill descriptions contain unquoted ``: `` mid-prose
    ("…workflows: deciding…"), which is invalid YAML but obvious intent."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    block = text[4:end]
    try:
        data = yaml.safe_load(block)
        if isinstance(data, dict):
            return data
    except yaml.YAMLError:
        pass
    return _lenient_frontmatter(block)


def _lenient_frontmatter(block: str) -> dict | None:
    """key: value per top-level line; unindented continuation-free format only
    (the Agent Skills frontmatter is a flat two-key mapping)."""
    out: dict = {}
    cur = None
    for ln in block.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", ln)
        if m and not ln[:1].isspace():
            cur = m.group(1)
            out[cur] = m.group(2).strip()
        elif cur and ln.strip():
            out[cur] = (out[cur] + " " + ln.strip()).strip()
    return out or None


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end < 0:
        return text
    return text[end + 4:].lstrip("\n")


def first_sentence(desc: str) -> str:
    """The gloss before the 'Use when …' trigger prose — the compact index."""
    d = " ".join((desc or "").split())
    m = re.search(r"\.(?:\s|$)", d)
    return d[:m.start() + 1] if m else d


# ---- registry (.chipchamp/skills.json) ----------------------------------------


def registry_path(ws) -> Path:
    return Path(ws.dot) / "skills.json"


def load_registry(ws) -> list[str]:
    p = registry_path(ws)
    if not p.exists():
        return []
    try:
        data = load_json(p)
    except Exception:
        return []
    dirs = data.get("dirs", []) if isinstance(data, dict) else []
    return [d for d in dirs if isinstance(d, str)]


def _save_registry(ws, dirs: list[str]) -> None:
    atomic_write(registry_path(ws), dump_json({"dirs": dirs}))


def add_registry_dir(ws, path: str) -> list[str]:
    p = str(Path(path).expanduser().resolve())
    dirs = load_registry(ws)
    if p not in dirs:
        dirs.append(p)
        _save_registry(ws, dirs)
    return dirs


def remove_registry_dir(ws, path: str | None = None, all: bool = False) -> list[str]:
    if all:
        _save_registry(ws, [])
        return []
    dirs = load_registry(ws)
    if path:
        p = str(Path(path).expanduser().resolve())
        dirs = [d for d in dirs if d != p]
        _save_registry(ws, dirs)
    return dirs


# ---- discovery ----------------------------------------------------------------


def skill_dirs(ws) -> list[tuple[Path, str]]:
    """Ordered, deduped (base_dir, source) pairs to scan."""
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(p: Path, source: str) -> None:
        try:
            key = str(p.expanduser().resolve())
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        if Path(key).is_dir():
            out.append((Path(key), source))

    add(Path(ws.dot) / "skills", "workspace")
    cfg = (ws.config.get("skills", {}) or {}) if hasattr(ws, "config") else {}
    for d in cfg.get("dirs", []) or []:
        p = Path(str(d)).expanduser()
        if not p.is_absolute():
            p = Path(ws.root) / p
        add(p, "config")
    for d in load_registry(ws):
        add(Path(d), "registry")
    return out


def discover(ws) -> dict[str, Skill]:
    return discover_report(ws).skills


def discover_report(ws) -> DiscoveryReport:
    """Scan every layer; first occurrence of a name wins (workspace beats
    config beats registry; within a layer, listing/insertion order)."""
    rep = DiscoveryReport()
    for base, source in skill_dirs(ws):
        try:
            subdirs = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for d in subdirs:
            manifest = d / "SKILL.md"
            if not manifest.is_file():
                continue
            try:
                fm = parse_frontmatter(manifest.read_text(errors="replace"))
            except OSError:
                fm = None
            if not fm or not str(fm.get("name", "")).strip():
                rep.warnings.append(f"{manifest}: missing/invalid frontmatter "
                                    f"(need name + description) — skipped")
                continue
            name = str(fm["name"]).strip()
            if name != d.name:
                rep.warnings.append(f"{manifest}: frontmatter name '{name}' != "
                                    f"directory '{d.name}' (frontmatter wins)")
            if name in rep.skills:
                rep.shadowed.append(f"{name} ({d}) shadowed by "
                                    f"({rep.skills[name].dir})")
                continue
            rep.skills[name] = Skill(
                name=name, description=str(fm.get("description", "")).strip(),
                dir=d, source=source)
    return rep


# ---- index + body -------------------------------------------------------------


def skills_index(ws, mode: str = "compact", task: str = "") -> str:
    """The system-prompt index. semantic (the default) = only the skills
    relevant to `task`, picked by embedding similarity against a local
    /v1/embeddings endpoint (O(top_k) prompt tokens instead of O(corpus));
    compact = name + first sentence for every skill; full = the whole
    description (best static-trigger fidelity, most tokens); off = ''.
    Semantic degrades to compact whenever there is no task text, no
    reachable embedding endpoint, or any embedding error — never raises."""
    if mode == "off":
        return ""
    if mode == "semantic":
        try:
            from .skills_semantic import semantic_index
            text = semantic_index(ws, task)
            if text:
                return text
        except Exception:
            pass  # no endpoint / network hiccup -> static index
        mode = "compact"
    lines = []
    for s in discover(ws).values():
        gloss = s.description if mode == "full" else first_sentence(s.description)
        lines.append(f"- {s.name}: {gloss}")
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > _INDEX_CAP:
        text = text[:_INDEX_CAP] + "\n…[index truncated — skill.list has the rest]"
    return text


def skill_body(skill: Skill) -> dict:
    """Full playbook + the bundled files it may reference."""
    text = (skill.dir / "SKILL.md").read_text(errors="replace")
    files = sorted(str(f.relative_to(skill.dir))
                   for f in skill.dir.rglob("*")
                   if f.is_file() and f.name != "SKILL.md")
    return {"name": skill.name, "description": skill.description,
            "body": strip_frontmatter(text), "files": files,
            "dir": str(skill.dir)}


def resolve_ref(skill: Skill, relpath: str) -> Path | None:
    """A bundled file, confined to the skill directory (no absolute paths,
    no ``..`` escapes, no symlinks pointing outside)."""
    if not relpath or Path(relpath).is_absolute():
        return None
    try:
        root = skill.dir.resolve()
        cand = (skill.dir / relpath).resolve()
    except OSError:
        return None
    if not cand.is_relative_to(root):
        return None
    return cand if cand.is_file() else None
