"""Evidence bundles (SPEC §8.8, §11.3): the product, not the prose.

Assembled and *signed by the platform* from job records — the model contributes
only the clearly-labeled narrative section (FR-EVID-01). The signature is a hash
over the machine-generated content so tampering (or a fabricated gate result) is
detectable. This is what rides on every MR and makes review cheap.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..util.hashing import sha256_text
from ..util.jsonio import atomic_write, dump_json


@dataclass
class EvidenceBundle:
    id: str
    created_ts: float
    task_class: str
    change_summary: dict
    semantic_diff: dict
    gate_table: list[dict]
    sim_results: list[dict]
    coverage_delta: dict
    formal_lec: list[dict]
    wave_snapshots: list[dict]
    repro_commands: list[str]
    cost: dict
    antigaming: list[dict]
    narrative: str = "(model narrative not provided)"
    signature: str = ""

    def machine_payload(self) -> dict:
        """Everything except the (untrusted) narrative — this is what's signed."""
        d = {k: getattr(self, k) for k in (
            "id", "created_ts", "task_class", "change_summary", "semantic_diff",
            "gate_table", "sim_results", "coverage_delta", "formal_lec",
            "wave_snapshots", "repro_commands", "cost", "antigaming")}
        return d

    def sign(self) -> None:
        self.signature = "sha256:" + sha256_text(dump_json(self.machine_payload(), sort_keys=True))

    def verify(self) -> bool:
        expected = "sha256:" + sha256_text(dump_json(self.machine_payload(), sort_keys=True))
        return self.signature == expected

    def to_dict(self) -> dict:
        return {**self.machine_payload(), "narrative": self.narrative,
                "signature": self.signature}

    def to_markdown(self) -> str:
        L = [f"# Evidence bundle {self.id}",
             f"_task class: **{self.task_class}** · signed `{self.signature[:24]}…`_", ""]
        L.append("## Gate table")
        L.append("| gate | status | evidence | detail |")
        L.append("|---|---|---|---|")
        for g in self.gate_table:
            mark = "✅" if g["status"] == "pass" else ("❌" if g["status"] == "fail" else "⚠️")
            L.append(f"| {g['name']} | {mark} {g['status']} | {g.get('evidence','')} | {g.get('detail','')} |")
        L.append("")
        if self.sim_results:
            L.append("## Simulation")
            for s in self.sim_results:
                L.append(f"- `{s['job']}` {s['test']} seed={s.get('seed')} → **{s['status']}**")
            L.append("")
        if self.coverage_delta:
            L.append("## Coverage")
            L.append(f"- {self.coverage_delta.get('summary','(none)')}")
            L.append("")
        if self.formal_lec:
            L.append("## Formal / LEC")
            for f in self.formal_lec:
                L.append(f"- `{f['job']}` {f['kind']} → **{f['verdict']}**")
            L.append("")
        if self.antigaming:
            L.append("## Anti-gaming findings")
            for a in self.antigaming:
                ack = "acknowledged" if a.get("acknowledged") else "**UNACKNOWLEDGED**"
                L.append(f"- {a['kind']} in `{a['file']}` — {a['detail']} ({ack})")
            L.append("")
        L.append("## Reproduction")
        L.append("```bash")
        L += self.repro_commands
        L.append("```")
        L.append("")
        L.append(f"## Cost\n- {self.cost}")
        L.append("")
        L.append("## Model narrative _(not machine-verified)_")
        L.append(self.narrative)
        return "\n".join(L)


def build_bundle(bundle_id: str, task_class: str, *, change_summary: dict,
                 semantic_diff: dict, gate_report, jobs: list,
                 coverage_delta: Optional[dict] = None,
                 wave_snapshots: Optional[list] = None,
                 repro_commands: Optional[list] = None,
                 antigaming: Optional[list] = None,
                 narrative: str = "", created_ts: float = 0.0) -> EvidenceBundle:
    sim_results = [{"job": j.id, "test": j.input_files[-1] if j.input_files else "",
                    "seed": j.seed, "status": j.status}
                   for j in jobs if j.kind == "sim"]
    formal_lec = [{"job": j.id, "kind": j.kind,
                   "verdict": j.result.get("status", j.status)}
                  for j in jobs if j.kind in ("lec", "formal")]
    total_cpu = sum(j.cpu_seconds for j in jobs)
    total_lic = sum(j.license_seconds for j in jobs)
    b = EvidenceBundle(
        id=bundle_id, created_ts=created_ts or time.time(), task_class=task_class,
        change_summary=change_summary, semantic_diff=semantic_diff,
        gate_table=[g.__dict__ for g in gate_report.gates],
        sim_results=sim_results, coverage_delta=coverage_delta or {},
        formal_lec=formal_lec, wave_snapshots=wave_snapshots or [],
        repro_commands=repro_commands or [],
        cost={"cpu_hours": round(total_cpu / 3600, 4),
              "license_hours": round(total_lic / 3600, 4),
              "jobs": len(jobs)},
        antigaming=antigaming or [],
        narrative=narrative or "(model narrative not provided)")
    b.sign()
    return b


def save_bundle(bundle: EvidenceBundle, bundles_dir: str) -> str:
    d = Path(bundles_dir) / bundle.id
    atomic_write(d / "bundle.json", dump_json(bundle.to_dict()))
    atomic_write(d / "bundle.md", bundle.to_markdown())
    return str(d)
