"""Semantic skill triggering: cached corpus embeddings, cosine + name-mention
ranking, task-scoped prompt index, graceful fallback to the static index."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from chipchamp import skills as sk
from chipchamp import skills_semantic as sem


def _mk_ws(tmp_path, config_text=""):
    from chipchamp.config import Workspace
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "config.toml").write_text(config_text or "[project]\nname='t'\n")
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    return Workspace(str(tmp_path))


def _mk_skills(tmp_path) -> Path:
    root = tmp_path / "skills"
    for name, desc in (
        ("hw-cdc", "Clock-domain-crossing review. Use when auditing CDC, "
                   "synchronizers, or multi-clock designs."),
        ("fpga-flow", "FPGA implementation flow. Use when running synthesis "
                      "and place-and-route for an FPGA target."),
        ("sv-uvm", "UVM testbench methodology. Use when building UVM agents, "
                   "sequences, or scoreboards."),
    ):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody")
    return root


def _fake_embed(texts_axis):
    """Keyword -> axis fake embedder; deterministic, network-free."""
    calls = {"batches": [], "texts": 0}

    def fake(ws, texts, timeout):
        calls["batches"].append(list(texts))
        calls["texts"] += len(texts)
        out = []
        for t in texts:
            low = t.lower()
            vec = [0.05, 0.05, 0.05]
            for i, words in enumerate(texts_axis):
                if any(w in low for w in words):
                    vec[i] = 1.0
            out.append(vec)
        return out
    return fake, calls


AXES = (("cdc", "clock-domain", "clock domain"),
        ("fpga", "place-and-route"),
        ("uvm", "testbench"))


@pytest.fixture()
def sem_ws(tmp_path, monkeypatch):
    root = _mk_skills(tmp_path)
    ws = _mk_ws(tmp_path / "work",
                config_text=f"[project]\nname='t'\n[skills]\ndirs=['{root}']\n")
    fake, calls = _fake_embed(AXES)
    monkeypatch.setattr(sem, "_embed", fake)
    monkeypatch.setattr(sem, "resolve_endpoint", lambda w: ("fake://", "fake-embed"))
    sem._RESOLVED.clear()
    return ws, calls


def test_semantic_is_the_default_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIPCHAMP_SKILLS_INDEX", raising=False)
    ws = _mk_ws(tmp_path / "w")
    assert ws.skills_index_mode() == "semantic"
    ws2 = _mk_ws(tmp_path / "w2",
                 config_text="[project]\nname='t'\n[skills]\nindex='compact'\n")
    assert ws2.skills_index_mode() == "compact"


def test_select_ranks_by_similarity(sem_ws):
    ws, _ = sem_ws
    picked = sem.select(ws, "audit the clock domain crossings", sk.discover(ws))
    assert picked[0][0] == "hw-cdc"


def test_name_mention_beats_cosine(sem_ws):
    ws, _ = sem_ws
    # task is semantically CDC but explicitly names fpga-flow
    picked = sem.select(ws, "use fpga-flow to check the clock domain crossing",
                        sk.discover(ws))
    assert picked[0][0] == "fpga-flow"
    assert picked[0][1] > 1.0  # the mention bonus


def test_mention_is_word_bounded_not_substring():
    # a real, whole-token mention counts (both hyphen and spaced forms)
    assert sem._mentioned("fpga-flow", "please use fpga-flow now")
    assert sem._mentioned("sv-uvm", "build an sv uvm bench")
    # but a bare substring inside a larger word/compound must NOT count
    assert not sem._mentioned("run", "the pipeline is running fine")
    assert not sem._mentioned("fpga", "the fpgas are configured")
    assert not sem._mentioned("cdc", "the abcdcache is warm")
    assert not sem._mentioned("hw-cdc", "sub-hw-cdc-reset variant")


def test_short_name_does_not_hijack_selection(sem_ws, monkeypatch):
    # add a skill literally named "run"; a task merely containing "running"
    # must not force it to the top via a spurious mention bonus
    from chipchamp.config import Workspace
    ws, _ = sem_ws
    (Path(ws.root).parent / "skills" / "run").mkdir(parents=True, exist_ok=True)
    (Path(ws.root).parent / "skills" / "run" / "SKILL.md").write_text(
        "---\nname: run\ndescription: Launch the app. Use when running things.\n---\nx")
    picked = sem.select(ws, "audit the clock domain crossings while running",
                        sk.discover(ws))
    assert picked[0][0] == "hw-cdc"  # semantic winner, not the substring 'run'


def test_corpus_cache_reused_and_invalidated(sem_ws, tmp_path):
    ws, calls = sem_ws
    skills = sk.discover(ws)
    sem.corpus_vectors(ws, skills)
    assert calls["texts"] == 3  # first build embeds everything
    sem.corpus_vectors(ws, skills)
    assert calls["texts"] == 3  # cache hit: nothing re-embedded
    # change one description -> exactly one re-embed
    p = tmp_path / "skills" / "hw-cdc" / "SKILL.md"
    p.write_text(p.read_text().replace("multi-clock", "many-clock"))
    sem.corpus_vectors(ws, sk.discover(ws))
    assert calls["texts"] == 4
    data = json.loads(sem.cache_path(ws).read_text())
    assert set(data["entries"]) == {"hw-cdc", "fpga-flow", "sv-uvm"}


def test_prompt_carries_only_selected_skills(sem_ws, monkeypatch):
    ws, _ = sem_ws
    monkeypatch.setenv("CHIPCHAMP_SKILLS_INDEX", "semantic")
    from chipchamp.agent.prompts import build_system_prompt
    prompt = build_system_prompt(ws, "", "policy",
                                 task="run place-and-route for the fpga")
    assert "fpga-flow" in prompt
    assert "selected by relevance" in prompt
    assert "sv-uvm" not in prompt  # not relevant, not in the prompt


def test_no_task_or_no_endpoint_falls_back_to_compact(sem_ws, monkeypatch):
    ws, _ = sem_ws
    # no task text (REPL banner): static index with every skill
    idx = sk.skills_index(ws, "semantic", task="")
    assert "hw-cdc" in idx and "sv-uvm" in idx and "selected" not in idx
    # endpoint down: same fallback, never raises
    monkeypatch.setattr(sem, "_embed",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    idx2 = sk.skills_index(ws, "semantic", task="audit cdc")
    assert "hw-cdc" in idx2 and "sv-uvm" in idx2


def test_never_selects_nothing(sem_ws):
    ws, _ = sem_ws
    # a task matching no axis still yields the best 3 seeds
    picked = sem.select(ws, "write release notes", sk.discover(ws))
    assert len(picked) == 3


# ---- live (local embedding server present) ------------------------------------


def _lms_up() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:1234/v1/models",
                                    timeout=2) as r:
            return any("embed" in m.get("id", "")
                       for m in json.load(r).get("data", []))
    except Exception:
        return False


LIVE_SKILLS = Path(os.path.expanduser("~/skills"))


@pytest.mark.skipif(not (_lms_up() and (LIVE_SKILLS / "hw-cdc-reset").is_dir()),
                    reason="needs LM Studio embedding model + ~/skills corpus")
def test_live_semantic_pick(tmp_path):
    ws = _mk_ws(tmp_path / "work",
                config_text=f"[project]\nname='t'\n[skills]\n"
                            f"dirs=['{LIVE_SKILLS}']\n")
    sem._RESOLVED.clear()
    idx = sk.skills_index(ws, "semantic",
                          task="review the design for clock domain crossing "
                               "and reset synchronization issues")
    assert "hw-cdc-reset" in idx
    assert "selected by relevance" in idx
