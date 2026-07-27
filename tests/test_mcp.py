"""MCP server + client (SPEC §15.2): full round-trip over a real subprocess."""
from __future__ import annotations

import json
import sys

import pytest

from chipchamp.mcp import McpClient, McpServer, exposed_tools
from conftest import EXAMPLE


def test_exposed_surface_is_read_only():
    tools = exposed_tools()
    names = set(tools)
    assert "design.hierarchy" in names and "wave.when" in names
    # never expose mutation or completion authority over MCP
    for forbidden in ("fs.write", "fs.edit", "report.done", "evidence.bundle",
                      "lint.run", "sim.run", "regress.run"):
        assert forbidden not in names


def test_server_dispatch_inprocess():
    srv = McpServer(str(EXAMPLE))
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["serverInfo"]["name"] == "chipchamp"
    lst = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in lst["result"]["tools"]}
    assert "design__hierarchy" in names
    call = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "design__module",
                                  "arguments": {"module": "sync_fifo"}}})
    payload = json.loads(call["result"]["content"][0]["text"])
    assert payload["card"]["name"] == "sync_fifo"
    # unknown method -> JSON-RPC error, not a crash
    bad = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert bad["error"]["code"] == -32601


def test_client_server_roundtrip_subprocess():
    client = McpClient(
        "self", [sys.executable, "-m", "chipchamp.mcp.server",
                 "--root", str(EXAMPLE)])
    try:
        assert client.server_info.get("name") == "chipchamp"
        tools = client.list_tools()
        names = {t["name"] for t in tools}
        assert "design__hierarchy" in names and "design__cone" in names
        result = client.call("design__hierarchy", {"top": "soc_top", "depth": 1})
        assert result["top"] == "soc_top"
        assert result["hierarchy"]["module"] == "soc_top"
    finally:
        client.close()


def test_load_mcp_tools_wraps_as_agent_tools(tmp_path):
    cfg = {"mcp": {"servers": {"designdb": {
        "command": [sys.executable, "-m", "chipchamp.mcp.server",
                    "--root", str(EXAMPLE)]}}}}
    from chipchamp.mcp import load_mcp_tools
    tools, clients = load_mcp_tools(cfg)
    try:
        assert any(n.startswith("mcp.designdb.design.module") for n in tools)
        t = tools["mcp.designdb.design.module"]
        out = t.handler(None, module="counter")
        assert out["card"]["name"] == "counter"
        assert t.group == "mcp"
    finally:
        for c in clients:
            c.close()
