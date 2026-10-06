"""Design statistics from a checkpoint, headless.

usage: python -m chipchamp.adapters.rw_scripts.dcp_stats <design.dcp> <report.json>

Reports the part, the cell count by primitive type and by site family, the
sites and tiles used, the clock regions touched, and a structural routing
summary: nets with routing (PIPs) versus nets that have pins to connect but
no routing — the shape of "is this checkpoint actually routed" without
opening the vendor GUI.
"""
from __future__ import annotations

import sys
from collections import Counter

from ._common import cell_records, read_design, run


def main(argv: list[str]) -> dict:
    dcp = argv[0]
    d = read_design(dcp)
    dev = d.getDevice()
    cells = cell_records(d)
    by_type = Counter(c["type"] for c in cells)
    by_family = Counter(c.get("family", "unplaced") for c in cells)
    sites = {c["site"] for c in cells if c.get("site")}
    tiles = {c["tile"] for c in cells if c.get("tile")}
    regions = Counter(c["clock_region"] for c in cells if c.get("clock_region"))

    nets = {"total": 0, "routed": 0, "unrouted": 0, "static": 0, "clock": 0,
            "unrouted_names": []}
    for net in d.getNets():
        nets["total"] += 1
        if net.isStaticNet():
            nets["static"] += 1
            continue
        if net.isClockNet():
            nets["clock"] += 1
        pins = int(net.getPins().size())
        if net.hasPIPs():
            nets["routed"] += 1
        elif pins >= 2:
            nets["unrouted"] += 1
            if len(nets["unrouted_names"]) < 40:
                nets["unrouted_names"].append(str(net.getName()))

    top = ""
    try:
        top = str(d.getNetlist().getTopCell().getName())
    except Exception:  # noqa: BLE001 — informational only
        pass
    return {
        "checkpoint": dcp, "part": str(d.getPartName()),
        "device": str(dev.getName()), "series": str(dev.getSeries().name()),
        "top": top,
        "cells": {"total": len(cells),
                  "placed": sum(1 for c in cells if c["placed"]),
                  "unplaced": sum(1 for c in cells if not c["placed"]),
                  "by_family": dict(by_family),
                  "by_type": dict(by_type.most_common(60))},
        "sites_used": len(sites), "tiles_used": len(tiles),
        "clock_regions": dict(regions),
        "device_grid": {"columns": int(dev.getColumns()), "rows": int(dev.getRows())},
        "nets": nets,
        "fully_routed": nets["unrouted"] == 0,
    }


if __name__ == "__main__":
    sys.exit(run(main, sys.argv[1:]))
