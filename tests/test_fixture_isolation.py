"""The example workspace in the repo is read-only to the test suite. Every
fixture-driven write lands in a per-test copy."""
from __future__ import annotations

import os

from conftest import EXAMPLE, REAL_EXAMPLE


def test_ctx_points_at_a_copy_not_the_repo(ctx):
    root = os.path.realpath(ctx.ws.root)
    assert root != os.path.realpath(str(REAL_EXAMPLE))
    assert not root.startswith(os.path.realpath(str(REAL_EXAMPLE)))
    assert (ctx.ws.root / "rtl" / "soc_top.sv").exists() if hasattr(ctx.ws.root, "exists") \
        else os.path.exists(os.path.join(ctx.ws.root, "rtl", "soc_top.sv"))


def test_writes_through_tools_never_reach_the_repo(ctx):
    from chipchamp.tools.fs_tools import fs_write
    out = fs_write(ctx, "work/rtl/zz_isolation_probe.sv", "module zz; endmodule\n")
    assert out.get("status") == "added"
    assert os.path.exists(os.path.join(ctx.ws.root, "work/rtl/zz_isolation_probe.sv"))
    assert not os.path.exists(REAL_EXAMPLE / "work/rtl/zz_isolation_probe.sv")


def test_session_copy_is_outside_the_repo():
    assert not str(EXAMPLE).startswith(str(REAL_EXAMPLE))
    assert (EXAMPLE / "rtl" / "soc_top.sv").exists()


def test_a_fresh_copy_carries_no_generated_state(tmp_path):
    from conftest import copy_example
    root = copy_example(tmp_path / "soc")
    assert (root / "rtl" / "soc_top.sv").exists()
    assert not (root / ".chipchamp" / "runs").exists()      # no inherited job history
    assert not (root / "work").exists()                      # no inherited scratch
    assert not (root / ".chipchamp" / "db-soc.json").exists()


def test_each_test_gets_its_own_copy(example_root, tmp_path):
    assert os.path.realpath(example_root).startswith(os.path.realpath(str(tmp_path)))
