#!/usr/bin/env python
"""Read the e2e matrix and say what actually happened.

Two questions, kept separate on purpose:

1. **Did the feature work?** A per-scenario check reading real state (a cached
   job record, a persisted note, a budget_block event) — never the model's
   prose. This is a property of chipchamp.
2. **Did the model use it?** The same check, read the other way. A feature can
   be correct and still never be reached by a 4B model that does not call
   tools. That is a fact about the model, and conflating the two would let a
   weak model look like a broken feature.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LAB = Path("/tmp/claude-1000/-home-riadh-chipchamp/"
           "c9b08b22-9d5d-4475-84f2-e44d61dd6535/scratchpad/lab")

# the check that is the POINT of each scenario (others are incidental)
PRIMARY = {"cache": "job_cache_hit", "disclosure": "tools_load_called",
           "detach": "detached_job", "notebook": "note_recorded",
           "spawn": "spawn_called", "budget": "budget_blocked"}


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LAB / "results.json"
    rows = json.loads(path.read_text())
    models, scenarios = [], []
    for r in rows:
        if r["model"] not in models:
            models.append(r["model"])
        if r["scenario"] not in scenarios and r["scenario"] != "-":
            scenarios.append(r["scenario"])

    print(f"# e2e autopsy — {len(rows)} run(s)\n")

    print("## feature reached, per model × scenario")
    head = "| model | " + " | ".join(scenarios) + " |"
    print(head)
    print("|" + "---|" * (len(scenarios) + 1))
    for m in models:
        cells = []
        for s in scenarios:
            r = next((x for x in rows
                      if x["model"] == m and x["scenario"] == s), None)
            if r is None:
                cells.append("–")
            elif r["outcome"] in ("harness_error", "unavailable"):
                cells.append("ERR")
            else:
                hit = (r.get("checks") or {}).get(PRIMARY[s])
                cells.append("YES" if hit else f"no ({r['outcome']})")
        print(f"| {m.replace('ollama:', '')} | " + " | ".join(cells) + " |")

    print("\n## did the FEATURE work anywhere (chipchamp's property)")
    for s in scenarios:
        hits = [r for r in rows if r["scenario"] == s
                and (r.get("checks") or {}).get(PRIMARY[s])]
        who = ", ".join(sorted(h["model"].replace("ollama:", "") for h in hits))
        print(f"- **{s}** ({PRIMARY[s]}): "
              + (f"YES — {who}" if hits else "NOT OBSERVED"))

    print("\n## cost + outcome per run")
    print("| model | scenario | outcome | steps | wall s | tokens | ctx peak | "
          "compactions |")
    print("|" + "---|" * 8)
    for r in rows:
        m = r.get("metrics") or {}
        print(f"| {r['model'].replace('ollama:', '')} | {r['scenario']} | "
              f"{r['outcome']} | {m.get('steps', '-')} | "
              f"{m.get('wall_s', r.get('wall_s', '-'))} | "
              f"{m.get('tokens_total', '-')} | "
              f"{m.get('context_chars_max', '-')} | "
              f"{m.get('compactions', '-')} |")

    print("\n## tools each model actually reached for")
    for m in models:
        used: dict[str, int] = {}
        for r in rows:
            if r["model"] != m:
                continue
            for name, st in ((r.get("metrics") or {}).get("tools") or {}).items():
                used[name] = used.get(name, 0) + st["calls"]
        top = ", ".join(f"{k}×{v}" for k, v in
                        sorted(used.items(), key=lambda kv: -kv[1])[:12])
        print(f"- **{m.replace('ollama:', '')}**: {top or '(none)'}")


if __name__ == "__main__":
    main()
