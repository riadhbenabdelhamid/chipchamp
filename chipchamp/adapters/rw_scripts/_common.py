"""Shared bits for the RapidWright scripts: JVM bootstrap, report writing,
and the site/cell classification the placement picture and the stats share."""
from __future__ import annotations

import json
import sys
import time
import traceback


def write_report(path: str, report: dict) -> None:
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1, default=str)


def run(main, argv: list[str]) -> int:
    """Run `main(argv) -> report dict`, always leaving a JSON report at
    argv[1] (even on failure) and returning a process exit code."""
    out = argv[1] if len(argv) > 1 else "report.json"
    t0 = time.time()
    try:
        report = main(argv)
        report.setdefault("seconds", round(time.time() - t0, 2))
        write_report(out, report)
        return 0
    except BaseException as e:  # noqa: BLE001 — the report IS the error channel
        write_report(out, {"error": f"{type(e).__name__}: {e}",
                           "traceback": traceback.format_exc()[-4000:],
                           "seconds": round(time.time() - t0, 2)})
        return 1


def start_rapidwright():
    """Import the pip package (which boots the JVM) and return the Java
    classes the scripts use. Raised ImportError names the fix."""
    try:
        import rapidwright  # noqa: F401  — boots the JVM via JPype
    except ImportError as e:
        raise ImportError("the `rapidwright` package is not importable from "
                          f"{sys.executable}: pip install -e '.[rapidwright]' "
                          "(or point CHIPCHAMP_RAPIDWRIGHT_PYTHON at an "
                          f"interpreter that has it) — {e}") from e
    from com.xilinx.rapidwright.design import Design  # type: ignore
    return Design


def read_design(dcp: str):
    Design = start_rapidwright()
    return Design.readCheckpoint(dcp)


# Site-type families, for colouring and for the per-family tallies. Matched
# as prefixes of the site type enum name (SLICEL, RAMB36E2, DSP48E2, HPIOB…).
FAMILIES = (
    ("logic", ("SLICE",)),
    ("bram", ("RAMB", "URAM")),
    ("dsp", ("DSP",)),
    ("io", ("IOB", "HPIOB", "HRIO", "HDIOB", "IOB33", "IOB18", "ILOGIC", "OLOGIC",
            "IDELAY", "ODELAY", "BITSLICE", "RIU")),
    ("clock", ("BUFG", "BUFH", "BUFR", "MMCM", "PLL", "BUFCE", "BUFGCE", "BUFGCTRL")),
    ("hard", ("PCIE", "GTH", "GTY", "GTX", "CMAC", "ILKN", "HBM", "PS8", "PS7")),
)


def family_of(site_type: str) -> str:
    st = (site_type or "").upper()
    for fam, prefixes in FAMILIES:
        if any(st.startswith(p) for p in prefixes):
            return fam
    return "other"


def cell_records(design) -> list[dict]:
    """One record per cell: name, type, placed, site, site type, tile x/y,
    clock region, family. Java → plain Python at the boundary."""
    out = []
    for cell in design.getCells():
        site = cell.getSite()
        rec = {"name": str(cell.getName()), "type": str(cell.getType()),
               "placed": site is not None}
        if site is not None:
            tile = site.getTile()
            cr = tile.getClockRegion()
            st = str(site.getSiteTypeEnum().name())
            rec.update(site=str(site.getName()), site_type=st,
                       family=family_of(st), tile=str(tile.getName()),
                       x=int(tile.getTileXCoordinate()),
                       y=int(tile.getTileYCoordinate()),
                       clock_region=str(cr.getName()) if cr is not None else "")
        out.append(rec)
    return out
