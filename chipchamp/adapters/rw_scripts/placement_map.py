"""A picture of where the design landed on the device, headless.

usage: python -m chipchamp.adapters.rw_scripts.placement_map <design.dcp>
       <report.json> <map.png> [highlight.json]

Every placed cell is drawn at its tile's (x, y) on the device grid, coloured
by site family (logic / bram / dsp / io / clock / hard), shaded by how many
cells share the tile. `highlight.json` is a list of {"match": <substring or
regex over the hierarchical cell name>, "color": "#rrggbb", "label": "..."}
entries; matching cells are drawn on top in that colour. The report carries
the bounding box, the per-family counts and the legend — the picture is for
the human, the numbers are for the agent.
"""
from __future__ import annotations

import json
import re
import sys

from ._common import cell_records, read_design, run

COLORS = {"logic": (70, 190, 90), "bram": (70, 120, 230), "dsp": (200, 70, 200),
          "io": (240, 170, 40), "clock": (60, 200, 220), "hard": (200, 60, 60),
          "other": (150, 150, 150)}
BG, GRID = (12, 14, 18), (26, 30, 36)


def _scale(cols: int, rows: int, max_px: int = 1800) -> int:
    return max(2, min(8, max_px // max(cols, rows, 1)))


def main(argv: list[str]) -> dict:
    dcp, _report, png = argv[0], argv[1], argv[2]
    highlights = json.load(open(argv[3])) if len(argv) > 3 and argv[3] else []
    from PIL import Image, ImageDraw
    d = read_design(dcp)
    dev = d.getDevice()
    cols, rows = int(dev.getColumns()), int(dev.getRows())
    cells = [c for c in cell_records(d) if c["placed"]]
    s = _scale(cols, rows)
    img = Image.new("RGB", (cols * s + 2, rows * s + 2), BG)
    draw = ImageDraw.Draw(img)
    for x in range(0, cols, 10):
        draw.line([(x * s, 0), (x * s, rows * s)], fill=GRID)
    for y in range(0, rows, 10):
        draw.line([(0, y * s), (cols * s, y * s)], fill=GRID)

    per_tile: dict[tuple[int, int], list[dict]] = {}
    for c in cells:
        per_tile.setdefault((c["x"], c["y"]), []).append(c)
    densest = max((len(v) for v in per_tile.values()), default=1)
    for (x, y), group in per_tile.items():
        fam = max((c["family"] for c in group), key=lambda f: list(COLORS).index(f) if f in COLORS else 99)
        r, g, b = COLORS.get(fam, COLORS["other"])
        k = 0.45 + 0.55 * (len(group) / densest)
        # the device grid's y grows upward; image y grows downward
        px, py = x * s, (rows - 1 - y) * s
        draw.rectangle([px, py, px + s - 1, py + s - 1],
                       fill=(int(r * k), int(g * k), int(b * k)))

    matched: dict[str, int] = {}
    for h in highlights:
        pat = str(h.get("match", ""))
        if not pat:
            continue
        rx = re.compile(pat) if any(ch in pat for ch in ".*[]()|^$") else None
        color = h.get("color", "#ffff00")
        if isinstance(color, str) and color.startswith("#") and len(color) == 7:
            rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        else:
            rgb = (255, 255, 0)
        n = 0
        for c in cells:
            if (rx.search(c["name"]) if rx else pat in c["name"]):
                px, py = c["x"] * s, (rows - 1 - c["y"]) * s
                draw.rectangle([px, py, px + s - 1, py + s - 1], fill=rgb)
                n += 1
        matched[h.get("label") or pat] = n
    img.save(png)

    xs = [c["x"] for c in cells]
    ys = [c["y"] for c in cells]
    # A small design on a big device is two coloured tiles in a dark field.
    # Also write a crop around the used area, magnified, so the picture
    # says something at a glance; the full map keeps the device context.
    zoom_png = ""
    if cells:
        pad = 6
        x0, x1 = max(0, min(xs) - pad), min(cols - 1, max(xs) + pad)
        y0, y1 = max(0, min(ys) - pad), min(rows - 1, max(ys) + pad)
        box = (x0 * s, (rows - 1 - y1) * s, (x1 + 1) * s, (rows - y0) * s)
        crop = img.crop(box)
        zoom = max(1, 900 // max(crop.width, crop.height, 1))
        if (x1 - x0 + 1) * (y1 - y0 + 1) < 0.25 * cols * rows:
            crop = crop.resize((crop.width * zoom, crop.height * zoom), Image.NEAREST)
            zoom_png = png.replace(".png", ".zoom.png")
            crop.save(zoom_png)
    fam_counts: dict[str, int] = {}
    for c in cells:
        fam_counts[c["family"]] = fam_counts.get(c["family"], 0) + 1
    return {
        "checkpoint": dcp, "part": str(d.getPartName()), "png": png,
        "zoom_png": zoom_png,
        "device_grid": {"columns": cols, "rows": rows, "pixels_per_tile": s},
        "placed_cells": len(cells), "tiles_used": len(per_tile),
        "bbox": ({"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)}
                 if cells else None),
        "by_family": fam_counts,
        "legend": {k: "#%02x%02x%02x" % v for k, v in COLORS.items()},
        "highlights": matched,
    }


if __name__ == "__main__":
    sys.exit(run(main, sys.argv[1:]))
