"""Read-only dashboard (SPEC §7.5): a self-contained static HTML page generated
from the workspace's job records, evidence bundles and coverage snapshots. No
server, no design source — shareable evidence, not a control surface."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from . import brand
from .config import Workspace

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2128;--mut:#57606a;--line:#d8dee4;
--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--acc:#0d7d88}
body{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:1000px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:13px;text-transform:uppercase;
letter-spacing:.08em;color:var(--mut);margin:28px 0 10px}
.meta{color:var(--mut);font-size:12.5px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);text-align:left;padding:8px 12px;border-bottom:1px solid var(--line)}
td{padding:8px 12px;border-bottom:1px solid var(--line);font-family:ui-monospace,monospace;font-size:12.5px}
tr:last-child td{border-bottom:none}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.warn{color:var(--warn)}.tag{display:inline-block;padding:1px 8px;border:1px solid var(--line);border-radius:10px;font-size:11px;color:var(--mut)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.card .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
"""


def generate_dashboard(ws: Workspace) -> str:
    ctx_runner_dir = ws.dot / "runs"
    from .jobs import JobRunner
    runner = JobRunner(str(ctx_runner_dir), ws.registry())
    jobs = runner.list_jobs()
    passed = sum(1 for j in jobs if j.status == "passed")
    failed = sum(1 for j in jobs if j.status in ("failed", "error", "timeout"))
    cpu_h = sum(j.cpu_seconds for j in jobs) / 3600
    lic_h = sum(j.license_seconds for j in jobs) / 3600

    bundles = []
    bdir = ws.dot / "bundles"
    if bdir.is_dir():
        for d in sorted(bdir.iterdir()):
            f = d / "bundle.json"
            if f.exists():
                try:
                    bundles.append(json.loads(f.read_text()))
                except json.JSONDecodeError:
                    continue

    rows = []
    for j in jobs[-40:][::-1]:
        cls = "ok" if j.status == "passed" else ("bad" if j.status in
                                                 ("failed", "error") else "warn")
        rows.append(f"<tr><td>{j.id}</td><td class='{cls}'>{j.status}</td>"
                    f"<td>{j.kind}</td><td>{j.adapter}</td>"
                    f"<td>{html.escape(j.summary[:60])}</td>"
                    f"<td>{j.duration_s:.1f}s</td></tr>")

    brows = []
    for b in bundles[::-1][:20]:
        gates = b.get("gate_table", [])
        ok = all(g.get("status") == "pass" for g in gates)
        cls = "ok" if ok else "bad"
        brows.append(
            f"<tr><td>{html.escape(b.get('id',''))}</td>"
            f"<td class='{cls}'>{'all pass' if ok else 'incomplete'}</td>"
            f"<td>{html.escape(b.get('task_class',''))}</td>"
            f"<td>{len(b.get('sim_results',[]))} sims</td>"
            f"<td>{html.escape(str(b.get('signature',''))[:26])}…</td></tr>")

    proj = ws.config.get("project", {}).get("name", ws.root)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{brand.APP_NAME} — {html.escape(proj)}</title><style>{_CSS}</style></head><body>
<div class="wrap">
<h1>{brand.APP_NAME} dashboard — {html.escape(proj)}</h1>
<div class="meta">read-only evidence view · generated {stamp} · no design source on this page</div>
<h2>Totals</h2>
<div class="cards">
<div class="card"><div class="k">jobs</div><div class="v">{len(jobs)}</div></div>
<div class="card"><div class="k">passed</div><div class="v" style="color:var(--ok)">{passed}</div></div>
<div class="card"><div class="k">failed</div><div class="v" style="color:var(--bad)">{failed}</div></div>
<div class="card"><div class="k">cpu-hours</div><div class="v">{cpu_h:.2f}</div></div>
<div class="card"><div class="k">license-hours</div><div class="v">{lic_h:.2f}</div></div>
<div class="card"><div class="k">bundles</div><div class="v">{len(bundles)}</div></div>
</div>
<h2>Evidence bundles</h2>
<table><tr><th>id</th><th>gates</th><th>class</th><th>sims</th><th>signature</th></tr>
{''.join(brows) or '<tr><td colspan=5>none yet</td></tr>'}</table>
<h2>Recent jobs</h2>
<table><tr><th>id</th><th>status</th><th>kind</th><th>adapter</th><th>summary</th><th>wall</th></tr>
{''.join(rows) or '<tr><td colspan=6>none yet</td></tr>'}</table>
</div></body></html>"""
    out = ws.dot / "dashboard.html"
    out.write_text(page)
    return str(out)
