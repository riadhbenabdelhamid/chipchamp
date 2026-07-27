"""`caps` meta tool + registry role report — the agent's runtime view of what
this machine can actually run (adapters, per-role selection, SV front end)."""
from __future__ import annotations

import shutil

from chipchamp.tools import all_tools


def test_caps_registered_with_schema():
    t = all_tools()["caps"]
    assert t.group == "meta" and t.permission == "read"
    assert set(t.schema["properties"]) == {"role", "available_only"}


def test_caps_reports_skills(ctx):
    r = all_tools()["caps"].handler(ctx)
    assert "skills" in r
    assert set(r["skills"]) >= {"count", "dirs", "index"}
    assert r["skills"]["index"] in ("semantic", "compact", "full", "off")


def test_caps_reports_roles_and_adapters(ctx):
    r = all_tools()["caps"].handler(ctx)
    # every role the registry knows shows a selection verdict
    for role in ("sim", "lint", "synth", "formal", "sta", "pnr", "efpga-fabulous"):
        assert role in r["roles"], f"role {role} missing from caps"
        assert set(r["roles"][role]) >= {"selected", "available", "pinned", "candidates"}
    names = {m["adapter"] for m in r["adapters"]}
    assert {"verilator", "yosys", "questa", "librelane"} <= names
    # availability in the report matches reality on this machine
    by_name = {m["adapter"]: m for m in r["adapters"]}
    for tool_name, binary in (("verilator", "verilator"), ("yosys", "yosys")):
        assert by_name[tool_name]["available"] == bool(shutil.which(binary))
    assert r["sv_frontend"] in ("slang", "pragmatic")


def test_caps_role_filter_and_available_only(ctx):
    r = all_tools()["caps"].handler(ctx, role="sim")
    assert set(r["roles"]) == {"sim"}
    assert all("sim" in m["roles"] for m in r["adapters"])
    # unavailable is computed before the available_only filter hides entries
    r2 = all_tools()["caps"].handler(ctx, available_only=True)
    assert all(m["available"] for m in r2["adapters"])
    hidden = {m["adapter"] for m in r2["adapters"]}
    assert not hidden & set(r2["unavailable"])
    # commercial glue (questa/vcs/primetime) is not installed here -> reported
    if not shutil.which("qrun"):
        assert "questa" in r2["unavailable"]


def test_caps_unknown_role_is_an_error_not_empty(ctx):
    r = all_tools()["caps"].handler(ctx, role="quantum")
    assert "error" in r and "quantum" in r["error"]


def test_role_report_selection_matches_for_role(ctx):
    rep = ctx.registry.role_report()
    for role, v in rep.items():
        a = ctx.registry.for_role(role)
        assert v["selected"] == (a.name if a else None)
        if v["available"]:
            assert a is not None and a.available()


def test_role_report_honors_config_pin(example_root):
    from chipchamp.adapters.registry import AdapterRegistry
    reg = AdapterRegistry(selection={"sim": "icarus"})
    rep = reg.role_report()
    assert rep["sim"]["pinned"] == "icarus"
    assert rep["sim"]["candidates"][0] == "icarus"
    if shutil.which("iverilog"):
        assert rep["sim"]["selected"] == "icarus" and rep["sim"]["available"]
