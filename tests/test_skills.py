"""Agent Skills (SPEC §15.3): SKILL.md parsing, layered discovery + registry,
prompt index, the skill.* tools, path confinement, and injection posture.
Live test against the user's real corpus at ~/skills when present."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from chipchamp import skills as sk
from chipchamp.tools import all_tools

FIX = Path(__file__).parent / "fixtures" / "skills"


def _mk_ws(tmp_path, config_text=""):
    """A minimal real Workspace on a tmp dir."""
    from chipchamp.config import Workspace
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True, exist_ok=True)
    (dot / "config.toml").write_text(config_text or "[project]\nname='t'\n")
    (tmp_path / "rtl").mkdir(exist_ok=True)
    (tmp_path / "rtl" / "t.sv").write_text("module t(input logic clk); endmodule\n")
    return Workspace(str(tmp_path))


def _ctx_for(ws):
    from chipchamp.tools.context import ToolContext
    return ToolContext(ws)


# ---- parsing -------------------------------------------------------------------


def test_parse_frontmatter_strict_and_lenient():
    strict = "---\nname: a\ndescription: does a thing. Use when x.\n---\nbody"
    fm = sk.parse_frontmatter(strict)
    assert fm == {"name": "a", "description": "does a thing. Use when x."}
    # the real-corpus case: unquoted ': ' mid-description is invalid YAML —
    # the lenient fallback must still recover both keys
    lenient = ("---\nname: b\ndescription: chaining — for workflows: deciding "
               "boundaries, defining artifacts.\n---\n# B\n")
    fm2 = sk.parse_frontmatter(lenient)
    assert fm2 and fm2["name"] == "b" and "deciding boundaries" in fm2["description"]
    assert sk.parse_frontmatter("no frontmatter here") is None
    assert sk.parse_frontmatter("---\nnever closed") is None


def test_strip_frontmatter_and_first_sentence():
    assert sk.strip_frontmatter("---\nname: x\n---\n# Title\nbody") == "# Title\nbody"
    d = "Bring up an APB peripheral. Use when integrating a new slave."
    assert sk.first_sentence(d) == "Bring up an APB peripheral."
    assert sk.first_sentence("no trailing period") == "no trailing period"


# ---- discovery layers + registry -------------------------------------------------


def test_discovery_precedence_and_shadowing(tmp_path):
    # same skill name in all three layers: workspace > config > registry
    for layer in ("ws", "cfg", "reg"):
        d = tmp_path / layer / "dupe"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: dupe\ndescription: from {layer}. Use when testing.\n---\nbody")
    ws = _mk_ws(tmp_path / "work",
                config_text=f"[project]\nname='t'\n[skills]\ndirs=['{tmp_path}/cfg']\n")
    # workspace-implicit layer
    wsd = Path(ws.dot) / "skills" / "dupe"
    wsd.mkdir(parents=True)
    (wsd / "SKILL.md").write_text(
        "---\nname: dupe\ndescription: from workspace. Use when testing.\n---\nbody")
    sk.add_registry_dir(ws, str(tmp_path / "reg"))

    rep = sk.discover_report(ws)
    assert rep.skills["dupe"].source == "workspace"
    assert len(rep.shadowed) == 2  # cfg and reg copies both shadowed
    # remove the workspace copy: config wins next
    (wsd / "SKILL.md").unlink()
    assert sk.discover(ws)["dupe"].source == "config"


def test_registry_roundtrip_and_dedupe(tmp_path):
    ws = _mk_ws(tmp_path)
    d = str(FIX)
    assert sk.load_registry(ws) == []
    sk.add_registry_dir(ws, d)
    sk.add_registry_dir(ws, d)  # dedupe
    assert sk.load_registry(ws) == [str(Path(d).resolve())]
    sk.remove_registry_dir(ws, d)
    assert sk.load_registry(ws) == []
    sk.add_registry_dir(ws, d)
    sk.remove_registry_dir(ws, all=True)
    assert sk.load_registry(ws) == []


def test_missing_frontmatter_skipped_with_warning(tmp_path):
    bad = tmp_path / "lib" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("# no frontmatter at all\n")
    ws = _mk_ws(tmp_path / "work")
    sk.add_registry_dir(ws, str(tmp_path / "lib"))
    rep = sk.discover_report(ws)
    assert "broken" not in rep.skills
    assert any("missing/invalid frontmatter" in w for w in rep.warnings)


# ---- index + prompt ---------------------------------------------------------------


def _fixture_ws(tmp_path, index="compact"):
    ws = _mk_ws(tmp_path, config_text=(
        f"[project]\nname='t'\n[skills]\nindex='{index}'\ndirs=['{FIX}']\n"))
    return ws


def test_index_modes(tmp_path):
    ws = _fixture_ws(tmp_path)
    compact = sk.skills_index(ws, "compact")
    assert "- apb-bringup: Bring up an APB peripheral" in compact
    assert "Use when" not in compact.split("apb-bringup")[1].split("\n")[0]
    full = sk.skills_index(ws, "full")
    assert "Use when integrating a new APB slave" in full
    assert sk.skills_index(ws, "off") == ""


def test_system_prompt_carries_index(tmp_path, monkeypatch):
    # per-workspace config must decide here, not the suite-wide env pin
    monkeypatch.delenv("CHIPCHAMP_SKILLS_INDEX", raising=False)
    from chipchamp.agent.prompts import build_system_prompt
    ws = _fixture_ws(tmp_path)
    sp = build_system_prompt(ws, "", "policy")
    assert "## Skills (expert playbooks — load on demand)" in sp
    assert "- cdc-audit:" in sp and "skill.use" in sp
    ws_off = _fixture_ws(tmp_path / "off", index="off")
    assert "## Skills" not in build_system_prompt(ws_off, "", "policy")


# ---- tools ------------------------------------------------------------------------


def test_skill_tools_on_fixture_corpus(tmp_path):
    ctx = _ctx_for(_fixture_ws(tmp_path))
    t = all_tools()
    lst = t["skill.list"].handler(ctx)
    assert {"apb-bringup", "cdc-audit", "poisoned-skill"} <= \
        {s["name"] for s in lst["skills"]}
    use = t["skill.use"].handler(ctx, name="cdc-audit")
    assert "design.domains" in use["body"]
    assert use["files"] == ["references/checklist.md"]
    ref = t["skill.read"].handler(ctx, name="cdc-audit",
                                  path="references/checklist.md")
    assert "two-flop sync" in ref["content"]
    # unknown name → close-match help
    miss = t["skill.use"].handler(ctx, name="cdc-adit")
    assert "cdc-audit" in miss["error"]


def test_skill_read_confinement(tmp_path):
    ctx = _ctx_for(_fixture_ws(tmp_path))
    t = all_tools()
    for evil in ("../apb-bringup/SKILL.md", "/etc/passwd", "../../../etc/passwd"):
        r = t["skill.read"].handler(ctx, name="cdc-audit", path=evil)
        assert "error" in r, evil
    # symlink escaping the skill dir must not resolve
    link_dir = tmp_path / "lib" / "sneaky"
    link_dir.mkdir(parents=True)
    (link_dir / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: s. Use when never.\n---\nbody")
    os.symlink("/etc/passwd", link_dir / "out.md")
    sk.add_registry_dir(ctx.ws, str(tmp_path / "lib"))
    r = t["skill.read"].handler(ctx, name="sneaky", path="out.md")
    assert "error" in r


def test_poisoned_skill_flagged_by_injection_scanner(tmp_path):
    from chipchamp.policy.injection import scan_text
    ws = _fixture_ws(tmp_path)
    body = sk.skill_body(sk.discover(ws)["poisoned-skill"])["body"]
    assert scan_text(body), "injection heuristics must fire on the poisoned body"


# ---- live corpus -------------------------------------------------------------------

CORPUS = Path.home() / "skills"


@pytest.mark.skipif(not CORPUS.is_dir(), reason="~/skills corpus not present")
def test_live_corpus_discovery(tmp_path):
    ws = _mk_ws(tmp_path)
    sk.add_registry_dir(ws, str(CORPUS))
    rep = sk.discover_report(ws)
    assert len(rep.skills) >= 80
    assert not rep.warnings, rep.warnings[:3]
    for s in rep.skills.values():
        assert s.name and s.description
        assert s.name == s.dir.name  # corpus convention
    # progressive disclosure: fpga-targets bundles references/
    if "fpga-targets" in rep.skills:
        body = sk.skill_body(rep.skills["fpga-targets"])
        assert any(f.startswith("references/") for f in body["files"])
        ref = sk.resolve_ref(rep.skills["fpga-targets"], body["files"][0])
        assert ref and ref.is_file()
