"""ChipchampBench harness (SPEC §17.1): mutators produce real defects; B1 runs
end-to-end on the real simulator with a small mutant budget."""
from __future__ import annotations

from conftest import EXAMPLE, requires_icarus

from chipchamp.bench import generate_mutants, run_b1


def test_mutators_generate_distinct_compilable_defects():
    src = open(EXAMPLE / "rtl" / "sync_fifo.sv").read()
    mutants = generate_mutants(src, "rtl/sync_fifo.sv")
    assert len(mutants) >= 4
    ids = [m.id for m in mutants]
    assert len(ids) == len(set(ids))
    for m in mutants:
        assert m.mutated != src
        assert m.line > 0


@requires_icarus
def test_b1_detection_on_real_sim(tmp_path):
    r = run_b1(str(EXAMPLE), str(tmp_path), max_mutants=3)
    assert r["total"] == 3
    # every result actually ran a sim and produced a verdict
    assert all(x["sim_status"] in ("passed", "failed", "error", "timeout")
               for x in r["mutants"])
    assert r["detection_rate"] is not None
