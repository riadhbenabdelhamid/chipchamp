"""Filelist (.f) ingestion: nested -f, +incdir+, +define+."""
from __future__ import annotations

from chipchamp.filelist import parse_filelist


def test_nested_filelist_and_switches(tmp_path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule")
    (tmp_path / "rtl" / "b.sv").write_text("module b; endmodule")
    (tmp_path / "inc").mkdir()
    sub = tmp_path / "sub.f"
    sub.write_text("// sub\n+incdir+inc\n+define+WIDTH=8\nrtl/b.sv\n")
    top = tmp_path / "top.f"
    top.write_text("// top\nrtl/a.sv\n-f sub.f\n+define+SIM\n")
    fs = parse_filelist(str(top))
    names = {p.rsplit("/", 1)[-1] for p in fs.sources}
    assert names == {"a.sv", "b.sv"}
    assert fs.defines["WIDTH"] == "8"
    assert fs.defines["SIM"] == "1"
    assert any(d.endswith("inc") for d in fs.incdirs)


def test_glob_in_filelist(tmp_path):
    (tmp_path / "rtl").mkdir()
    for n in ("x.sv", "y.sv"):
        (tmp_path / "rtl" / n).write_text(f"module {n[0]}; endmodule")
    f = tmp_path / "f.f"
    f.write_text("rtl/*.sv\n")
    fs = parse_filelist(str(f))
    assert len({p.rsplit("/", 1)[-1] for p in fs.sources}) == 2
