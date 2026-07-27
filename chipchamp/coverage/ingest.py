"""Coverage ingest (SPEC §8.7): Verilator ``.dat`` code coverage and a native
functional-coverage JSON (what a cocotb-coverage export or the platform emits).

Commercial UCIS/UCDB/VDB import (URG/IMC) lands via vendor export in M2; the
normalized model is identical so downstream tools are stack-neutral.
"""
from __future__ import annotations

import re

from ..util.jsonio import load_json
from .model import Bin, CoverageModel


def ingest_verilator_dat(path: str, test: str = "") -> CoverageModel:
    """Parse Verilator's coverage.dat (SystemC::Coverage-3 line format)."""
    m = CoverageModel(sources=[path])
    try:
        text = open(path, "r", errors="replace").read()
    except OSError:
        return m
    for line in text.splitlines():
        if not line.startswith("C "):
            continue
        mm = re.match(r"C\s+'(.*)'\s+(\d+)\s*$", line)
        if not mm:
            continue
        items_raw, count = mm.group(1), int(mm.group(2))
        items = {}
        for part in items_raw.split("\x01"):
            if "\x02" in part:
                k, v = part.split("\x02", 1)
                items[k] = v
        f = items.get("f", items.get("filename", ""))
        ln = items.get("l", items.get("lineno", "0"))
        page = items.get("page", "")
        ctype = "toggle" if "toggle" in page else ("branch" if "branch" in page else "line")
        hier = items.get("h", "")
        bid = f"{ctype}:{f}:{ln}:{items.get('o', items.get('c',''))}:{hier}"
        m.add_bin(Bin(id=bid, group=ctype, point=f, name=f"{f}:{ln}",
                      count=count, source=f"{f}:{ln}", kind=ctype), test=test or None)
    return m


def ingest_functional_json(path: str, test: str = "") -> CoverageModel:
    """Native functional-coverage JSON:
    {"covergroups":[{"name","source","section",
       "coverpoints":[{"name","bins":[{"name","hits","weight"}]}]}]}"""
    m = CoverageModel(sources=[path])
    data = load_json(path)
    for cg in data.get("covergroups", []):
        gname = cg["name"]
        section = cg.get("section", "")
        for cp in cg.get("coverpoints", []):
            pname = cp["name"]
            for b in cp.get("bins", []):
                bid = f"{gname}::{pname}::{b['name']}"
                m.add_bin(Bin(id=bid, group=gname, point=pname, name=b["name"],
                              count=int(b.get("hits", 0)), weight=int(b.get("weight", 1)),
                              source=cg.get("source", ""), section=section,
                              kind="functional"), test=test or None)
    return m
