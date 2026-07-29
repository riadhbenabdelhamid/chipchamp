"""MCP client (SPEC §15.2): consume external MCP servers as agent tools.

Config (``.chipchamp/config.toml``)::

    [mcp.servers.tracker]
    command = ["python", "-m", "my_tracker_mcp"]

    [mcp.servers.wavelets]                     # a terminal waveform viewer
    command = "python3 /path/to/wavelet.py --mcp"

A server may answer with pictures as well as text. Those are written to
``<dot>/mcp-images/`` and the result carries their PATHS — never the base64,
which would cost thousands of context characters per call. ``[ui] mcp_images``
(auto | image | off) decides whether the REPL draws them inline (kitty /
iTerm2 / WezTerm) or just links them.

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
                 timeout: float = 20.0, image_dir: Optional[str] = None):
        self.name = name
        self.command = command
        self.timeout = timeout
        # where image blocks are spilled; defaults beside the cwd so a picture
        # outlives the call that produced it and can be reopened later
        from pathlib import Path
        self.image_dir = Path(image_dir or ((Path(cwd) if cwd else Path.cwd())
                                            / ".chipchamp" / "mcp-images"))
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
        """Unwrap the content envelope into one agent-facing result.

        An MCP reply may carry several blocks — wavelets' image renderers send
        the picture AND a caption. Returning at the first text block dropped the
        picture entirely; keeping the picture *inline* would be worse, because a
        PNG is kilobytes of base64 and the transcript is a budgeted resource
        (see agent/working_set.py). So an image is spilled to a file and only
        its PATH travels: the terminal can render it, the model can cite it, and
        the context pays a few dozen characters instead of a few thousand.
        """
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        blocks = result.get("content") or []
        images = [b for b in blocks if b.get("type") == "image" and b.get("data")]
        out: Optional[dict] = None
        for item in blocks:
            if item.get("type") == "text":
                try:
                    out = json.loads(item["text"])
                except json.JSONDecodeError:
                    out = {"text": item["text"]}
                if not isinstance(out, dict):
                    out = {"text": item["text"]}
                break
        if out is None:
            out = {} if images else result
        saved = [self._spill_image(tool, b) for b in images]
        saved = [s for s in saved if s]
        if saved:
            out["images"] = saved
        return out

    def _spill_image(self, tool: str, block: dict) -> Optional[dict]:
        """Write one image block beside the workspace and describe it."""
        import base64
        import time
        mime = block.get("mimeType") or "image/png"
        ext = {"image/png": "png", "image/svg+xml": "svg",
               "image/jpeg": "jpg", "image/gif": "gif"}.get(mime, "bin")
        try:
            raw = base64.b64decode(block["data"], validate=False)
        except Exception:
            return None
        d = self.image_dir
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{self.name}-{tool}-{int(time.time() * 1000)}.{ext}"
            p.write_bytes(raw)
        except OSError:
            return None
        return {"path": str(p), "mime": mime, "bytes": len(raw)}

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()


def load_mcp_tools(config: dict, cwd: Optional[str] = None,
                   image_dir: Optional[str] = None) -> tuple[dict, list]:
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
            client = McpClient(name, cmd, cwd=cwd, image_dir=image_dir)
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
