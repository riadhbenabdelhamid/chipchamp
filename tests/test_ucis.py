"""UCIS XML + cocotb-coverage XML ingest → normalized CoverageModel: the
functional-coverage path is stack-neutral (FR-COV-01)."""
from __future__ import annotations

import os

from chipchamp.coverage import (CoverageService, ingest_cocotb_xml,
                               ingest_coverage, ingest_ucis_xml)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
UCIS = os.path.join(FIX, "ucis_axi.xml")


def test_ucis_covergroups_bins_counts():
    m = ingest_ucis_xml(UCIS)
    assert len(m.bins) == 7  # 3 + 2 coverpoint bins + 2 cross bins
    b = m.bins["axi_mstr_cg::burst_cp::wrap16"]
    assert b.count == 0 and not b.hit and b.kind == "functional"
    assert m.bins["axi_mstr_cg::burst_cp::fixed"].count == 120
    assert m.bins["axi_mstr_cg::burst_x_size::<incr,b4>"].count == 31


def test_ucis_source_citation_and_weight():
    m = ingest_ucis_xml(UCIS)
    # source resolves through the <sourceFiles> table
    assert m.bins["axi_mstr_cg::burst_cp::fixed"].source == "verif/axi_cov.sv:12"
    # per-point weight (size_cp weight=2)
    assert m.bins["axi_mstr_cg::size_cp::b1"].weight == 2


def test_ucis_test_attribution_from_history_nodes():
    m = ingest_ucis_xml(UCIS)
    assert "axi_burst_test" in m.per_test
    hit = m.per_test["axi_burst_test"]
    assert "axi_mstr_cg::burst_cp::fixed" in hit
    assert "axi_mstr_cg::burst_cp::wrap16" not in hit  # unhit not attributed


def test_ucis_holes_via_service():
    svc = CoverageService(ingest_ucis_xml(UCIS))
    holes = svc.holes(kind="functional")
    ids = {h["bin"] for h in holes["holes"]}
    assert ids == {"axi_mstr_cg::burst_cp::wrap16",
                   "axi_mstr_cg::burst_x_size::<wrap16,b4>"}
    # nearest-test heuristic points at the sibling-hitting test
    wrap = next(h for h in holes["holes"] if h["bin"].endswith("wrap16"))
    assert wrap["nearest_test"]["test"] == "axi_burst_test"


def test_ucis_baseline_gate_regression():
    base = CoverageService(ingest_ucis_xml(UCIS))
    cur_model = ingest_ucis_xml(UCIS)
    cur_model.bins["axi_mstr_cg::burst_cp::incr"].count = 0  # lose a bin
    verdict = CoverageService(cur_model).baseline_check(base.model)
    assert not verdict.ok and verdict.total_delta < 0


def test_cocotb_xml_ingest(tmp_path):
    xml = """<top abs_name="top" size="4" coverage="3" cover_percentage="75.0">
      <dff abs_name="top.dff" size="4" coverage="3" cover_percentage="75.0">
        <arst abs_name="top.dff.arst" size="2" coverage="1" weight="1" at_least="1"
              cover_percentage="50.0">
          <bin1 bin="0" hits="14" abs_name="top.dff.arst.0"/>
          <bin2 bin="1" hits="0" abs_name="top.dff.arst.1"/>
        </arst>
        <din abs_name="top.dff.din" size="2" coverage="2" weight="1" at_least="1"
             cover_percentage="100.0">
          <bin1 bin="0" hits="9" abs_name="top.dff.din.0"/>
          <bin2 bin="1" hits="11" abs_name="top.dff.din.1"/>
        </din>
      </dff>
    </top>"""
    p = tmp_path / "cocotb_cov.xml"
    p.write_text(xml)
    m = ingest_cocotb_xml(str(p), test="test_dff")
    assert len(m.bins) == 4
    assert m.bins["top.dff::arst::0"].count == 14
    assert not m.bins["top.dff::arst::1"].hit
    assert "top.dff::din::1" in m.per_test["test_dff"]


def test_dispatcher_sniffs_formats(tmp_path):
    m, fmt = ingest_coverage(UCIS)
    assert fmt == "ucis-xml" and m.bins
    p = tmp_path / "c.xml"
    p.write_text('<top abs_name="top"><x abs_name="top.x" weight="1">'
                 '<b bin="a" hits="1" abs_name="top.x.a"/></x></top>')
    m2, fmt2 = ingest_coverage(str(p))
    assert fmt2 == "cocotb-xml" and len(m2.bins) == 1
