"""Export a checkpoint to the FPGA Interchange Format.

usage: python -m chipchamp.adapters.rw_scripts.interchange_export <design.dcp>
       <report.json> <out_dir>

Writes <top>.netlist (logical netlist) and <top>.phys (placement + routing)
— the vendor-neutral form that nextpnr's fpga_interchange backend, VTR and
RapidWright itself all read. The report lists the files and their sizes.
"""
from __future__ import annotations

import os
import sys

from ._common import read_design, run


def main(argv: list[str]) -> dict:
    dcp, _report, out_dir = argv[0], argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    d = read_design(dcp)
    from com.xilinx.rapidwright.interchange import LogNetlistWriter, PhysNetlistWriter  # type: ignore
    top = str(d.getNetlist().getTopCell().getName()) or "design"
    log_path = os.path.join(out_dir, f"{top}.netlist")
    phys_path = os.path.join(out_dir, f"{top}.phys")
    LogNetlistWriter.writeLogNetlist(d.getNetlist(), log_path)
    PhysNetlistWriter.writePhysNetlist(d, phys_path)
    return {"checkpoint": dcp, "part": str(d.getPartName()), "top": top,
            "logical_netlist": log_path, "physical_netlist": phys_path,
            "bytes": {"logical_netlist": os.path.getsize(log_path),
                      "physical_netlist": os.path.getsize(phys_path)}}


if __name__ == "__main__":
    sys.exit(run(main, sys.argv[1:]))
