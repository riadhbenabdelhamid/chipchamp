"""Portable regression reports: one JUnit testcase per requested test and seed."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote
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


def sarif_report(jobs, workspace):
    """SARIF 2.1.0 diagnostics for lint and simulations, with portable locations."""
    rules, results, invocations = {}, [], []
    for rec in jobs:
        failed = rec.status != "passed"
        invocations.append({"executionSuccessful": rec.status in ("passed", "failed"),
                            "properties": {"job": rec.id, "status": rec.status}})
        diagnostics = list(rec.result.get("diagnostics", []))
        if failed and not any(d.get("severity") == "error" for d in diagnostics):
            diagnostics.append({"code": "JOB_" + rec.status.upper(), "severity": "error",
                                "message": rec.summary or rec.status})
        for diag in diagnostics:
            rule = f"{rec.adapter}/{diag.get('code') or diag.get('category') or rec.kind}"
            rules.setdefault(rule, {"id": rule})
            finding = {"ruleId": rule,
                       "level": {"error": "error", "warning": "warning"}.get(diag.get("severity"), "note"),
                       "message": {"text": str(diag.get("message") or rec.summary or rec.status)},
                       "properties": {"job": rec.id, "test": rec.test_name, "seed": rec.seed}}
            if diag.get("waiver"):
                finding["suppressions"] = [{"kind": "external", "status": "accepted",
                                             "justification": str(diag["waiver"])}]
            if diag.get("file"):
                path = Path(diag["file"])
                path = path if path.is_absolute() else Path(workspace) / path
                try:
                    relative = path.resolve().relative_to(Path(workspace).resolve())
                    location = {"artifactLocation": {"uri": quote(relative.as_posix(), safe="/"),
                                                     "uriBaseId": "%SRCROOT%"}}
                    if isinstance(diag.get("line"), int) and diag["line"] > 0:
                        location["region"] = {"startLine": diag["line"]}
                        if isinstance(diag.get("col"), int) and diag["col"] > 0:
                            location["region"]["startColumn"] = diag["col"]
                    finding["locations"] = [{"physicalLocation": location}]
                except ValueError:
                    pass  # external library: retain the diagnostic without a repository location
            results.append(finding)
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "chipchamp", "rules": list(rules.values())}},
                      "originalUriBaseIds": {"%SRCROOT%": {"uri": Path(workspace).resolve().as_uri() + "/"}},
                      "invocations": invocations, "results": results}]}


def regression_markdown(result, target, resumed=0, ordering=None):
    def escape(value):
        import html
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
    lines = [f"# Verification: {escape(target)}", "", escape(result.summary()),
             f"Resumed {resumed} previously passed case(s).", "",
             "| Test | Seed | Status | Job |", "| --- | ---: | --- | --- |"]
    lines.extend(f"| {escape(c.test)} | {c.seed} | {escape(c.job.status)} | {escape(c.job.id)} |"
                 for c in result.cases)
    from ..coverage.impact import explain
    details = explain(ordering)
    if details:
        lines.extend(["", "## Change impact", ""])
        lines.extend(escape(line) + "\n" for line in details)
    return "\n".join(lines) + "\n"
