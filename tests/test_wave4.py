"""Wave 4 — fan-out, memory across sessions, and the team surface.

These multiply what the earlier waves built: agent.spawn makes the existing
Orchestrator reachable by the model, the notebook lets session #40 know what
session #3 learned, and a shared job store turns wave 1's cache into something
a team pays for once.
"""
from __future__ import annotations

import pytest

from chipchamp.notebook import Notebook
from chipchamp.tools import all_tools


# ---- A: agent.spawn ---------------------------------------------------------

def _ctx_for_spawn(**kw):
    ctx = type("C", (), {})()
    ctx.gateway = type("G", (), {"ref": "m", "available": True})()
    ctx.loop = type("L", (), {"router": None, "on_event": lambda k, d: None})()
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def test_spawn_rejects_unknown_roles_and_names_the_real_ones():
    out = all_tools()["agent.spawn"].handler(
        _ctx_for_spawn(), tasks=[{"role": "wizard", "prompt": "x"}])
    assert "wizard" in out["error"]
    assert "triage-analyst" in out["roles"]      # so the next call can succeed


def test_a_subagent_cannot_spawn_subagents():
    """A subagent that could spawn is a fork bomb with a license pool attached."""
    out = all_tools()["agent.spawn"].handler(
        _ctx_for_spawn(is_subagent=True),
        tasks=[{"role": "reviewer", "prompt": "x"}])
    assert "cannot spawn subagents" in out["error"]


def test_a_fan_out_is_bounded():
    out = all_tools()["agent.spawn"].handler(
        _ctx_for_spawn(),
        tasks=[{"role": "reviewer", "prompt": "x"} for _ in range(20)])
    assert "too many at once" in out["error"]


def test_spawn_needs_a_model():
    ctx = _ctx_for_spawn()
    ctx.gateway = None
    out = all_tools()["agent.spawn"].handler(
        ctx, tasks=[{"role": "reviewer", "prompt": "x"}])
    assert "no model" in out["error"]


def test_no_tasks_is_an_error_not_a_silent_success():
    assert "error" in all_tools()["agent.spawn"].handler(_ctx_for_spawn(), tasks=[])


def test_subagents_still_cannot_report_done():
    """FR-MA-03 must survive the model being able to spawn: only the parent
    validates gates."""
    from chipchamp.agent.roles import builtin_roles
    for role in builtin_roles().values():
        assert "report.done" not in role.tools()
        assert "evidence.bundle" not in role.tools()
        assert "agent.spawn" not in role.tools()


def test_spawn_is_a_submit_permission():
    """It launches jobs by proxy, so plan mode must be able to gate it."""
    assert all_tools()["agent.spawn"].permission == "submit"


# ---- #4: the project notebook ----------------------------------------------

def test_a_finding_survives_the_session(tmp_path):
    Notebook(tmp_path).add("root_cause", "fifo overflow on seed 3",
                           "wr_ptr wraps one cycle early when almost_full is "
                           "registered", evidence="J-0342")
    rows = Notebook(tmp_path).entries()
    assert rows[0]["subject"] == "fifo overflow on seed 3"
    assert rows[0]["evidence"] == "J-0342"


def test_the_same_subject_updates_rather_than_accumulates(tmp_path):
    """A recurring investigation would otherwise fill the digest with echoes."""
    nb = Notebook(tmp_path)
    nb.add("root_cause", "fifo overflow", "first theory")
    nb.add("root_cause", "fifo overflow", "actually the almost_full register")
    rows = nb.entries()
    assert len(rows) == 1 and rows[0]["detail"] == "actually the almost_full register"
    assert "supersedes" in rows[0]


def test_a_different_kind_on_the_same_subject_is_a_separate_note(tmp_path):
    nb = Notebook(tmp_path)
    nb.add("root_cause", "fifo overflow", "wr_ptr wraps early")
    nb.add("fix_pattern", "fifo overflow", "register almost_full downstream")
    assert len(nb.entries()) == 2


def test_a_wrong_finding_can_be_deleted(tmp_path):
    """Worse than no memory is a confident wrong one, repeated into every
    prompt."""
    nb = Notebook(tmp_path)
    note = nb.add("gotcha", "spike below 0x1000", "cannot model it")
    assert nb.remove(note["id"]) is True
    assert nb.entries() == []
    assert nb.remove("N-nope") is False


def test_junk_notes_are_refused(tmp_path):
    nb = Notebook(tmp_path)
    assert "error" in nb.add("musing", "x", "y")          # not a kind
    assert "error" in nb.add("root_cause", "", "y")       # no subject
    assert "error" in nb.add("root_cause", "x", "")       # no detail
    assert nb.entries() == []


def test_the_digest_is_bounded_and_framed_as_evidence(tmp_path):
    """It is rent paid on every request, and a note is prior evidence — the
    model has to stay free to find it wrong."""
    nb = Notebook(tmp_path)
    for i in range(40):
        nb.add("gotcha", f"thing {i}", "d" * 400)
    d = nb.digest()
    assert d.count("\n- ") == 12
    assert "not as orders" in d
    assert len(d) < 4000


def test_an_empty_notebook_adds_nothing_to_the_prompt(tmp_path):
    assert Notebook(tmp_path).digest() == ""
    from chipchamp.agent.prompts import _notebook_section
    ws = type("W", (), {"dot": tmp_path})()
    assert _notebook_section(ws) == ""


def test_a_corrupt_notebook_never_breaks_the_prompt(tmp_path):
    (tmp_path / "notebook.json").write_text("[[[not json")
    from chipchamp.agent.prompts import _notebook_section
    ws = type("W", (), {"dot": tmp_path})()
    assert _notebook_section(ws) == ""


def test_the_digest_reaches_the_system_prompt(tmp_path):
    from chipchamp.agent.prompts import _notebook_section
    Notebook(tmp_path).add("constraint", "spike needs memory above 0x1000",
                           "its debug module owns 0x0-0x1000")
    ws = type("W", (), {"dot": tmp_path})()
    sec = _notebook_section(ws)
    assert "spike needs memory above 0x1000" in sec


# ---- F: the team surface ----------------------------------------------------

def test_a_shared_job_store_is_opt_in(tmp_path):
    """One person's flaky run becoming everyone's cached verdict is a real
    consequence — a workspace must not fall into sharing by accident."""
    import inspect

    from chipchamp import config
    src = inspect.getsource(config.Workspace.runner)
    assert '(self.config.get("jobs", {}) or {}).get("store", "")' in src
    assert 'str(self.dot / "runs")' in src        # default stays local


def test_nightly_composes_regression_history_and_dashboard():
    import inspect

    from chipchamp import cli
    src = inspect.getsource(cli.nightly.callback)   # click wraps the function
    for piece in ("RegressionManager", "flaky_tests", "generate_dashboard"):
        assert piece in src
    # non-zero exit on failures, so cron and CI can act on it
    assert "SystemExit(0 if not res.clusters else 1)" in src


def test_the_role_enum_reaches_the_schema_the_model_sees():
    """A live 4B run passed role="design"/"verification" — the LABELS from the
    request — and was rejected. The roles belonged in the schema, not only in
    the error message: a list you can discover solely by guessing wrong first
    is no help to a model that never retries."""
    from chipchamp.agent.roles import builtin_roles
    role = (all_tools()["agent.spawn"].schema["properties"]["tasks"]
            ["items"]["properties"]["role"])
    assert role["enum"] == sorted(builtin_roles())
    assert "reviewer" in role["enum"]


def test_the_role_enum_is_never_shipped_empty():
    """agent.roles imports the tool catalog, so resolving the names inside
    meta_tools is a cycle that fails quietly. An empty enum is worse than none
    — it forbids every value — so the filler removes the key instead."""
    from chipchamp.tools import catalog
    spawn = catalog.TOOLS["agent.spawn"]
    prop = (spawn.schema["properties"]["tasks"]["items"]["properties"]["role"])
    saved = prop.get("enum")
    try:
        prop["enum"] = []
        catalog._fill_role_enum()
        assert prop.get("enum")          # refilled, not left empty
    finally:
        if saved is not None:
            prop["enum"] = saved


def test_a_task_with_no_role_gets_a_usable_error(tmp_path):
    """A live gpt-oss run invented its own task shape ({tool, args}) with no
    `role` at all. The first version put None into the bad-role set and died in
    the join with a bare TypeError — so the model learned nothing and retried
    the same broken shape twice. Name what is wrong, and ship the catalogue."""
    out = all_tools()["agent.spawn"].handler(
        _ctx_for_spawn(), tasks=[{"label": "design", "tool": "lint.run"}])
    assert "(missing)" in out["error"]
    assert "reviewer" in out["roles"]          # how to get it right
    assert out["example"]["tasks"][0]["role"] == "reviewer"


def test_a_task_with_no_prompt_is_refused(tmp_path):
    """A subagent starts with no context beyond what the orchestrator writes,
    so an empty prompt is a subagent asked to guess the assignment."""
    out = all_tools()["agent.spawn"].handler(
        _ctx_for_spawn(),
        tasks=[{"role": "reviewer", "label": "design", "prompt": ""}])
    assert "no prompt" in out["error"] and "design" in out["error"]


def test_none_and_misspelt_roles_are_both_reported(tmp_path):
    out = all_tools()["agent.spawn"].handler(
        _ctx_for_spawn(),
        tasks=[{"role": "wizard", "prompt": "x"}, {"prompt": "y"}])
    assert "wizard" in out["error"] and "(missing)" in out["error"]
