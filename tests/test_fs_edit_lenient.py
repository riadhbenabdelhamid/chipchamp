"""fs.edit robustness: models reconstruct `old` from fs.read's numbered
render and lose exact whitespace — recovery must be safe and unique."""
from __future__ import annotations

from pathlib import Path

from chipchamp.tools import all_tools

FILE = (
    "module m;\n"
    "    // one early clr pulse hits (en,clr)=(1,1)\n"
    "    // purpose — the kind cov.holes should surface.\n"
    "    logic a;\n"
    "    logic a;\n"
    "endmodule\n"
)


def _ws(tmp_path):
    from chipchamp.config import Workspace
    dot = tmp_path / ".chipchamp"
    dot.mkdir(parents=True)
    (dot / "config.toml").write_text("[project]\nname='t'\n")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "m.sv").write_text(FILE)
    return Workspace(str(tmp_path))


def _ctx(ws):
    from chipchamp.tools.context import ToolContext
    return ToolContext(ws)


def edit(ctx, old, new):
    return all_tools()["fs.edit"].handler(ctx, path="rtl/m.sv", old=old, new=new)


def test_exact_still_works(tmp_path):
    ctx = _ctx(_ws(tmp_path))
    r = edit(ctx, "    logic a;\n    logic a;", "    logic a, b;")
    assert r.get("match") == "exact"


def test_whitespace_lenient_unique_match(tmp_path):
    ctx = _ctx(_ws(tmp_path))
    # model lost the 4-space indent on a MULTI-line old (substring can't hit)
    r = edit(ctx, "// one early clr pulse hits (en,clr)=(1,1)\n"
                  "// purpose — the kind cov.holes should surface.",
             "// purpose — now hit on purpose.\nassert_prop: assert(1);")
    assert r.get("match") == "whitespace-lenient", r
    text = (Path(ctx.ws.root) / "rtl" / "m.sv").read_text()
    # replacement re-indented to the file's 4-space level
    assert "    // purpose — now hit on purpose.\n    assert_prop:" in text


def test_pasted_line_numbers_are_stripped(tmp_path):
    ctx = _ctx(_ws(tmp_path))
    r = edit(ctx, "    2      // one early clr pulse hits (en,clr)=(1,1)",
             "// replaced")
    assert r.get("match") in ("whitespace-lenient", "line-numbers-stripped"), r
    assert "// replaced" in (Path(ctx.ws.root) / "rtl" / "m.sv").read_text()


def test_ambiguous_lenient_refused(tmp_path):
    ctx = _ctx(_ws(tmp_path))
    r = edit(ctx, "logic a;", "logic b;")  # two stripped-equal lines
    assert "error" in r and "not unique" in r["error"]


def test_not_found_teaches_with_raw_bytes(tmp_path):
    ctx = _ctx(_ws(tmp_path))
    r = edit(ctx, "// one early clr pulse hits (en,clr)=(9,9) totally off",
             "x")
    assert "error" in r
    assert "nearest_match" in r
    assert "(en,clr)=(1,1)" in r["nearest_match"]["raw"]
