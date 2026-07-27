"""MCP client (SPEC §15.2): consume external MCP servers as agent tools.

Config (``.chipchamp/config.toml``)::

    [mcp.servers.tracker]
    command = ["python", "-m", "my_tracker_mcp"]

Each server's tools are surfaced to the agent as ``mcp.<server>.<tool>`` with the
server-provided JSON schema, so the model can call an issue tracker, a docs
search, or another Chipchamp instance's design DB exactly like a native tool.
Transport: newline-delimited JSON-RPC 2.0 over the child's stdio.
"""
from __future__ import annotations

import json
import subprocess
import threading
from queue import Empty, Queue
from typing import Optional


class McpClient:
    def __init__(self, name: str, command: list[str], cwd: Optional[str] = None,
                 timeout: float = 20.0):
        self.name = name
        self.command = command
        self.timeout = timeout
        self._proc = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._q: Queue = Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._id = 0
        self.server_info: dict = {}
        self._initialize()

    # ---- transport -----------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if line:
                    try:
                        self._q.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except ValueError:
            pass  # closed

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": params or {}}
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        deadline = self.timeout
        while True:
            try:
                resp = self._q.get(timeout=deadline)
            except Empty:
                raise TimeoutError(f"MCP server '{self.name}': no response to {method}")
            if resp.get("id") == self._id:
                if "error" in resp:
                    raise RuntimeError(f"MCP {method}: {resp['error'].get('message')}")
                return resp.get("result", {})
            # unrelated message (notification) — keep waiting

    def _notify(self, method: str) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self._proc.stdin.flush()

    # ---- MCP methods -----------------------------------------------------------

    def _initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "chipchamp", "version": "0.1.0"}})
        self.server_info = result.get("serverInfo", {})
        self._notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        return self._request("tools/list").get("tools", [])

    def call(self, tool: str, arguments: dict) -> dict:
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        # unwrap the standard content envelope
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return {"text": item["text"]}
        return result

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()


def load_mcp_tools(config: dict, cwd: Optional[str] = None) -> tuple[dict, list]:
    """Spawn configured MCP servers and wrap their tools as agent Tool objects.
    Returns (tools_dict, clients) — callers own closing the clients."""
    from ..tools.base import Tool
    servers = (config.get("mcp") or {}).get("servers") or {}
    tools: dict = {}
    clients: list[McpClient] = []
    for name, spec in servers.items():
        cmd = spec.get("command")
        if isinstance(cmd, str):
            cmd = cmd.split()
        if not cmd:
            continue
        try:
            client = McpClient(name, cmd, cwd=cwd)
        except Exception:
            continue  # unreachable server: skip, don't break the agent
        clients.append(client)
        for t in client.list_tools():
            wire = t["name"]
            local_name = f"mcp.{name}.{wire.replace('__', '.')}"

            def handler(ctx, _c=client, _w=wire, **kwargs):
                return _c.call(_w, kwargs)

            tools[local_name] = Tool(
                name=local_name,
                description=f"[MCP:{name}] " + t.get("description", ""),
                cost="metered", permission="submit",
                schema=t.get("inputSchema") or {"type": "object", "properties": {}},
                handler=handler, group="mcp")
    return tools, clients
