"""cocotb testbench for the counter (SPEC playbook P4). Runs over Icarus via the
cocotb runner; a reference model checks every cycle."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


@cocotb.test()
async def counter_reference_check(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    dut.clr.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1

    model = 0
    width = int(dut.count.value.n_bits) if hasattr(dut.count.value, "n_bits") else 8
    mask = (1 << width) - 1
    dut.en.value = 1
    for i in range(40):
        clr = 1 if (i % 17 == 0 and i > 0) else 0
        dut.clr.value = clr
        await RisingEdge(dut.clk)
        await Timer(1, units="ns")
        if clr:
            model = 0
        else:
            model = 0 if model == mask else model + 1
        assert int(dut.count.value) == model, \
            f"cycle {i}: count={int(dut.count.value)} expected {model}"

    assert model > 0, "counter never advanced"
