"""Human-readable mutation evidence; JSON retains the complete case matrix."""
from html import escape


def markdown(report):
    def safe(value):
        return escape(str(value)).replace("|", "&#124;").replace("\n", " ")
    lines = ["# Mutation testing", "", f"Run: {safe(report['id'])} — {safe(report['status'])}", "",
             f"Jobs submitted: {report['jobs_submitted']} / {report['limits']['max_jobs']}", "",
             "Detection rate (detected / detected + survived): " + str(report.get("detection_rate")), "",
             safe(report["note"]), ""]
    if report.get("reason"):
        lines += [safe(report["reason"]), ""]
    lines += ["## Baseline", "", "| Test | Seed | Result | Job |", "|---|---|---|---|"]
    for c in report["baseline"]:
        lines.append("| " + " | ".join(safe(c.get(k, "")) for k in ("test", "seed", "status", "job")) + " |")
    lines += ["", "## Mutations", "", "| Source | Mutation | Result | Detected by | Patch |", "|---|---|---|---|---|"]
    for m in report["mutants"]:
        detected = m.get("detected_by", {})
        lines.append("| " + " | ".join(safe(v) for v in (
            f"{m['path']}:{m['line']}", m["description"], m["status"],
            f"{detected['test']} seed {detected['seed']}" if detected else "", m["patch"])) + " |")
    lines += ["", "## Case evidence", "",
              "| Mutation | Test | Seed | Result | Job | Summary |", "|---|---|---|---|---|---|"]
    for m in report["mutants"]:
        for case in m["cases"]:
            lines.append("| " + " | ".join(safe(v) for v in (
                m["id"], case["test"], case["seed"], case["status"],
                case.get("record", "no job submitted"), case.get("summary", ""))) + " |")
    lines += ["", "## Scope", "", "Tests: " + safe(", ".join(report["ordering"]["tests"])), "",
              "Ordering: " + safe(report["ordering"]["method"]), ""]
    for item in report["skipped"]:
        lines.append(f"- {safe(item['path'])}: {safe(item['reason'])}")
    if report["candidate_limit_reached"]:
        lines += ["", "More mutation sites exist; the candidate limit capped this experiment."]
    return "\n".join(lines) + "\n"
