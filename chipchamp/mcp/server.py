"""MCP server (SPEC §15.2): expose the design database and job store to other
tools over the Model Context Protocol.

Implements the MCP stdio transport — newline-delimited JSON-RPC 2.0 — with the
core methods (``initialize``, ``tools/list``, ``tools/call``, ``ping``). The
exposed surface is a **read-only allowlist** (design/wave/coverage queries, job
status/logs, repro, diagrams): an IDE, a script, or *another agent* can query the
elaborated hierarchy or a waveform without embedding Chipchamp — the design DB as
a platform asset. Mutating tools (fs.*, report.done, evidence.bundle) are never
exposed here.

Run:  chipchamp mcp-serve --root <workspace>     (or python -m chipchamp.mcp.server)
All logging goes to stderr; stdout carries only JSON-RPC messages.
"""
from __future__ import annotations

import json
import sys

from ..config import Workspace
from ..tools import all_tools
from ..tools.context import ToolContext

PROTOCOL_VERSION = "2024-11-05"

# read-only allowlist (SPEC §15.2: query surface, not a control surface)
_ALLOWED_GROUPS = {"design", "wave"}
_ALLOWED_NAMES = {"cov.summary", "cov.holes", "cov.attribution",
                  "job.status", "job.log", "regress.failures",
                  "sim.list_tests", "test.select", "repro",
                  "doc.blockdiagram", "policy.check"}


def exposed_tools() -> dict:
    cat = all_tools()
    out = {}
    for name, t in cat.items():
        if t.group in _ALLOWED_GROUPS or name in _ALLOWED_NAMES:
            out[name] = t
    return out


class McpServer:
    def __init__(self, root: str, target: str | None = None):
        self.ws = Workspace(root)
        self.ctx = ToolContext(self.ws, target=target)
        self.tools = exposed_tools()

    # ---- JSON-RPC dispatch ---------------------------------------------------

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "chipchamp", "version": _version()},
                }
            elif method in ("notifications/initialized", "initialized"):
                return None  # notification: no response
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [
                    {"name": n.replace(".", "__"),
                     "description": t.description,
                     "inputSchema": t.schema or {"type": "object", "properties": {}}}
                    for n, t in sorted(self.tools.items())]}
            elif method == "tools/call":
                result = self._call(params.get("name", ""),
                                    params.get("arguments") or {})
            else:
                return _err(msg_id, -32601, f"method not found: {method}")
        except Exception as e:  # a tool crash must not kill the server
            return _err(msg_id, -32603, f"{type(e).__name__}: {e}")
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call(self, wire_name: str, args: dict) -> dict:
        name = wire_name.replace("__", ".")
        tool = self.tools.get(name)
        if tool is None:
            return {"content": [{"type": "text",
                                 "text": json.dumps({"error": f"unknown tool {wire_name}"})}],
                    "isError": True}
        result = tool.handler(self.ctx, **args)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    # ---- stdio loop -----------------------------------------------------------

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        print(f"chipchamp mcp server: {len(self.tools)} read-only tools on "
              f"{self.ws.root}", file=sys.stderr)
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()


def _version() -> str:
    from .. import __version__
    return __version__


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Chipchamp MCP server (stdio)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--target", default=None)
    args = ap.parse_args(argv)
    McpServer(args.root, args.target).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
