"""UCIS functional-coverage ingest (SPEC §8.7, FR-COV-01).

Reads the Accellera **UCIS 1.0 XML interchange** format — what Questa
(``vcover report -format ucis``), VCS/urg and Xcelium/IMC export — plus the
**cocotb-coverage** XML export (the open-source functional path). Both land in
the same normalized :class:`CoverageModel`, so ``cov.holes``, attribution and
the baseline gate are stack-neutral: a covergroup hole looks identical whether
it came from a licensed UCDB or a cocotb run.

``ingest_coverage`` sniffs the format (verilator .dat / UCIS XML / cocotb XML /
native functional JSON) so callers never dispatch by hand.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from .ingest import ingest_functional_json, ingest_verilator_dat
from .model import Bin, CoverageModel


def _tag(el: ET.Element) -> str:
    """Local tag name, namespace stripped (UCIS writers disagree on xmlns)."""
    return el.tag.split("}")[-1]


def _walk(el: ET.Element):
    yield el
    for c in el:
        yield from _walk(c)


# ---- UCIS 1.0 XML interchange ------------------------------------------------


def ingest_ucis_xml(path: str, test: str = "") -> CoverageModel:
    """Covergroup/coverpoint/cross bins from a UCIS XML interchange file.

    Reads ``<cgInstance>/<coverpoint>/<coverpointBin>`` (+ ``<cross>/<crossBin>``)
    with counts from ``<contents coverageCount=...>``; source citations resolve
    through the ``<sourceFiles>`` table. Tests come from ``<historyNodes>``
    when the caller doesn't name one."""
    m = CoverageModel(sources=[path])
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return m
    files: dict[str, str] = {}
    history_test = ""
    for el in _walk(root):
        t = _tag(el)
        if t == "sourceFiles":
            files[el.get("id", "")] = el.get("fileName", "")
        elif t == "historyNodes" and not history_test:
            history_test = el.get("logicalName", "")
    test = test or history_test

    def source_of(el: ET.Element) -> str:
        for sub in _walk(el):
            if _tag(sub).lower().endswith("sourceid"):
                f = files.get(sub.get("file", ""), "")
                ln = sub.get("line", "")
                if f:
                    return f"{f}:{ln}"
        return ""

    def weight_of(el: ET.Element) -> int:
        for sub in el:
            if _tag(sub) == "options":
                try:
                    return int(float(sub.get("weight", "1")))
                except ValueError:
                    return 1
        return 1

    def count_of(bin_el: ET.Element) -> int:
        n = 0
        for sub in _walk(bin_el):
            if _tag(sub) == "contents":
                try:
                    n += int(float(sub.get("coverageCount", "0")))
                except ValueError:
                    pass
        return n

    for cg in (e for e in _walk(root) if _tag(e) == "cgInstance"):
        gname = cg.get("name", "covergroup")
        gsource = source_of(cg)
        for pt in cg:
            t = _tag(pt)
            if t not in ("coverpoint", "cross"):
                continue
            pname = pt.get("name", t)
            psource = source_of(pt) or gsource
            pweight = weight_of(pt)
            for b in pt:
                bt = _tag(b)
                if bt not in ("coverpointBin", "crossBin"):
                    continue
                bname = b.get("name", "bin")
                m.add_bin(Bin(
                    id=f"{gname}::{pname}::{bname}",
                    group=gname, point=pname, name=bname,
                    count=count_of(b), weight=pweight,
                    source=psource, kind="functional"), test=test or None)
    return m


# ---- cocotb-coverage XML export ----------------------------------------------


def ingest_cocotb_xml(path: str, test: str = "") -> CoverageModel:
    """cocotb-coverage ``export_to_xml`` format: nested elements carrying
    ``abs_name``; bins are sub-elements with ``bin`` + ``hits`` attributes."""
    m = CoverageModel(sources=[path])
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return m
    for el in _walk(root):
        if el.get("bin") is None or el.get("hits") is None:
            continue
        # bin's abs_name = <point abs_name>.<bin>; derive group/point from path
        parent_abs = ".".join(el.get("abs_name", "").split(".")[:-1])
        parts = [p for p in parent_abs.split(".") if p] or ["top"]
        point = parts[-1]
        group = ".".join(parts[:-1]) or "top"
        weight = int(float(el.get("weight", "1") or 1))
        try:
            hits = int(float(el.get("hits", "0")))
        except ValueError:
            hits = 0
        m.add_bin(Bin(
            id=f"{group}::{point}::{el.get('bin')}",
            group=group, point=point, name=el.get("bin", ""),
            count=hits, weight=weight, kind="functional"), test=test or None)
    return m


# ---- format dispatcher ---------------------------------------------------------


def ingest_coverage(path: str, test: str = "") -> tuple[CoverageModel, str]:
    """Sniff + ingest any supported coverage file. Returns (model, format)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dat":
        return ingest_verilator_dat(path, test=test), "verilator-dat"
    if ext == ".json":
        return ingest_functional_json(path, test=test), "functional-json"
    if ext in (".xml", ".ucis"):
        try:
            root_tag = _tag(ET.parse(path).getroot())
        except (ET.ParseError, OSError):
            return CoverageModel(sources=[path]), "unreadable"
        if root_tag == "UCIS":
            return ingest_ucis_xml(path, test=test), "ucis-xml"
        return ingest_cocotb_xml(path, test=test), "cocotb-xml"
    return CoverageModel(sources=[path]), "unknown"
