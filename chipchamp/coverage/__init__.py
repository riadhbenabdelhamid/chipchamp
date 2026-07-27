"""Coverage Service (SPEC §8.7)."""
from .ingest import ingest_functional_json, ingest_verilator_dat
from .model import Bin, CoverageModel
from .service import BaselineVerdict, CoverageService
from .ucis import ingest_cocotb_xml, ingest_coverage, ingest_ucis_xml

__all__ = [
    "CoverageModel",
    "Bin",
    "CoverageService",
    "BaselineVerdict",
    "ingest_verilator_dat",
    "ingest_functional_json",
    "ingest_ucis_xml",
    "ingest_cocotb_xml",
    "ingest_coverage",
]
