"""cocotb testbench with functional coverage (cocotb-coverage): the open-source
covergroup path. Samples control/value/rollover coverpoints + a cross every
cycle and exports the coverage DB as XML for `cov.load`.

Degrades to a plain reference-model check when cocotb_coverage is not
installed in the cocotb interpreter (e.g. oss-cad-suite's tabbypy3)."""
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

try:
    from cocotb_coverage.coverage import (CoverCross, CoverPoint, coverage_db)
    HAVE_COV = True
except ImportError:  # pragma: no cover - tabbypy3 path
    HAVE_COV = False

WIDTH = 8
MASK = (1 << WIDTH) - 1


def _quartile(count):
    if count == 0:
        return "zero"
    if count <= MASK // 4:
        return "low"
    if count <= 3 * MASK // 4:
        return "mid"
    return "high"


if HAVE_COV:
    @CoverPoint("top.counter.ctrl",
                xf=lambda en, clr, count, rolled: (en, clr),
                bins=[(0, 0), (0, 1), (1, 0), (1, 1)])
    @CoverPoint("top.counter.value",
                xf=lambda en, clr, count, rolled: _quartile(count),
                bins=["zero", "low", "mid", "high"])
    @CoverPoint("top.counter.rollover",
                xf=lambda en, clr, count, rolled: rolled,
                bins=[False, True])
    @CoverCross("top.counter.ctrl_x_value",
                items=["top.counter.ctrl", "top.counter.value"])
    def sample(en, clr, count, rolled):
        pass
else:  # pragma: no cover
    def sample(en, clr, count, rolled):
        pass


@cocotb.test()
async def counter_cov_check(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    dut.clr.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1

    model = 0
    dut.en.value = 1
    # one early clr pulse hits (en,clr)=(1,1), then free-run past 256 cycles so
    # the 8-bit counter wraps (rollover bin). (0,1) is left as a HOLE on
    # purpose — the kind cov.holes should surface.
    for i in range(300):
        clr = 1 if i == 30 else 0
        dut.clr.value = clr
        await RisingEdge(dut.clk)
        await Timer(1, units="ns")
        prev = model
        if clr:
            model = 0
        else:
            model = 0 if model == MASK else model + 1
        rolled = prev == MASK and model == 0
        assert int(dut.count.value) == model, \
            f"cycle {i}: count={int(dut.count.value)} expected {model}"
        sample(1, clr, model, rolled)

    if HAVE_COV:
        out = os.environ.get("CHIPCHAMP_COV_XML", "counter_cov_cocotb.xml")
        coverage_db.export_to_xml(filename=out)
        cocotb.log.info(f"coverage exported to {out}")
