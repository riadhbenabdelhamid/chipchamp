"""Portable regression reports: one JUnit testcase per requested test and seed."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .regression import RegressionResult


def _xml_text(value) -> str:
    # XML 1.0 forbids these characters even when escaped. Tool diagnostics may
    # contain terminal control bytes; preserve the message without invalid XML.
    return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]",
                  "\ufffd", str(value))


def regression_report(result: RegressionResult, target: str, plan=None) -> dict:
    return {"schema_version": 1, "run_id": result.run_id, "target": target,
            "total": result.total, "passed": result.passed, "failed": result.failed,
            "errored": result.errored, "summary": result.summary(), "plan": plan,
            "cases": [{"test": c.test, "seed": c.seed, "job": c.job.to_dict()}
                      for c in result.cases]}


def junit_xml(result: RegressionResult, target: str) -> str:
    suite = ET.Element("testsuite", {
        "name": _xml_text(result.run_id), "tests": str(result.total),
        "failures": str(result.failed), "errors": str(result.errored), "skipped": "0",
        "time": f"{sum(c.job.duration_s for c in result.cases):.6f}"})
    for case in result.cases:
        rec = case.job
        node = ET.SubElement(suite, "testcase", {
            "classname": _xml_text(target),
            "name": _xml_text(f"{case.test}[seed={case.seed}]"),
            "time": f"{rec.duration_s:.6f}"})
        properties = ET.SubElement(node, "properties")
        for key, value in {"job": rec.id, "seed": case.seed, "adapter": rec.adapter,
                           "tool_version": rec.tool_version, "inputs_hash": rec.inputs_hash,
                           "cached": str(rec.cached).lower()}.items():
            ET.SubElement(properties, "property", name=key, value=_xml_text(value))
        if rec.status != "passed":
            kind = "failure" if rec.status in ("failed", "timeout") else "error"
            failure = ET.SubElement(node, kind, {
                "type": _xml_text(rec.status), "message": _xml_text(rec.summary[:1000])})
            diagnostics = [str(d.get("message", "")) for d in rec.result.get("diagnostics", [])
                           if d.get("severity") == "error"]
            failure.text = _xml_text("\n".join([rec.summary, *diagnostics])[:12000])
        output = ET.SubElement(node, "system-out")
        output.text = _xml_text(f"job={rec.id}\nstatus={rec.status}\n{rec.summary}")
    ET.indent(suite)
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"
