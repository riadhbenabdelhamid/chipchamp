"""Two tools nothing exercised: lint.waive — the only approve-tier tool, whose
whole point is that the agent can DRAFT a waiver but never activate one — and
fpga.snapshot, the newest tool, whose refusal paths must be reachable without
a vendor toolchain on the machine."""
from __future__ import annotations

import os

import yaml

from chipchamp.agent.loop import AgentLoop
from chipchamp.agent.providers.base import ModelResponse
from chipchamp.jobs.record import JobRecord
from chipchamp.policy.waivers import load_waivers
from chipchamp.tools import all_tools


# ---- lint.waive --------------------------------------------------------------

def test_lint_waive_is_the_approve_tier():
    assert all_tools()["lint.waive"].permission == "approve"


def test_a_thin_justification_is_refused(ctx):
    out = all_tools()["lint.waive"].handler(ctx, rule="UNUSED", scope="rtl/x.sv",
                                            justification="meh")
    assert "too thin" in out["error"]
    assert not os.path.exists(ctx.ws.dot / "proposed_waivers.yaml")


def test_a_draft_is_staged_and_never_active(ctx):
    out = all_tools()["lint.waive"].handler(
        ctx, rule="UNUSED", scope="rtl/counter.sv",
        justification="the port is reserved for the next spin and intentionally unconnected")
    assert out["status"] == "proposed" and out["activated"] is False
    assert out["staged_in"].endswith(".chipchamp/proposed_waivers.yaml")
    entries = yaml.safe_load(open(ctx.ws.dot / "proposed_waivers.yaml"))
    assert entries[-1]["status"] == "proposed" and entries[-1]["approver"] is None
    # nothing under waivers/ (the protected, human-owned dir) and no active waiver
    assert not os.path.isdir(os.path.join(ctx.ws.root, "waivers"))
    assert all(w.status != "active" for w in load_waivers(ctx.ws.root))


def test_plan_mode_gates_a_waiver_draft(ctx):
    from chipchamp import cli
    assert "approve" in cli._GATE_PERMS

    class Gw:
        available, model, ref = True, "fake", "fake:test"
        script = [ModelResponse(text="", tool_calls=[
            {"id": "w1", "name": "lint__waive",
             "input": {"rule": "UNUSED", "scope": "rtl/counter.sv",
                       "justification": "reserved port, intentionally unconnected"}}]),
                  ModelResponse(text="ok", tool_calls=[])]

        def complete(self, system, transcript, tools, audit_items=None):
            return self.script.pop(0)

    seen = []
    loop = AgentLoop(ctx, Gw(), gate_permissions=set(cli._GATE_PERMS),
                     approver=lambda name, args, perm: seen.append((name, perm)) or False)
    loop.run("waive it")
    assert seen == [("lint.waive", "approve")]
    assert not os.path.exists(ctx.ws.dot / "proposed_waivers.yaml")   # denied = not drafted


# ---- fpga.snapshot -----------------------------------------------------------

def _fpga_record(ctx, job_id, flow, artifacts):
    rec = JobRecord(id=job_id, kind="fpga", adapter=flow, status="passed",
                    result={"ok": True, "status": "passed", "metrics": {"flow": flow, "top": "counter"}},
                    artifacts=artifacts)
    (ctx.runner.store_dir / job_id).mkdir(parents=True, exist_ok=True)
    ctx.runner._persist(rec)
    return rec


def test_snapshot_without_any_fpga_run_says_so(ctx):
    out = all_tools()["fpga.snapshot"].handler(ctx)
    assert "no fpga run recorded" in out["error"]


def test_snapshot_refuses_a_non_vivado_run(ctx):
    _fpga_record(ctx, "J-0001", "nextpnr",
                 {"report": os.path.join(ctx.ws.root, ".chipchamp/fpga/counter/report.json")})
    out = all_tools()["fpga.snapshot"].handler(ctx, module="counter")
    assert "not a vivado run" in out["error"]


def test_snapshot_needs_the_post_route_checkpoint(ctx):
    _fpga_record(ctx, "J-0002", "vivado",
                 {"report": os.path.join(ctx.ws.root, ".chipchamp/fpga/counter/report.json"),
                  "post_route_dcp": os.path.join(ctx.ws.root, ".chipchamp/fpga/counter/vivado/checkpoints/post_route.dcp")})
    out = all_tools()["fpga.snapshot"].handler(ctx, module="counter")
    assert "checkpoint not found" in out["error"]
