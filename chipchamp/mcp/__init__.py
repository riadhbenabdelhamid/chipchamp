"""MCP integration (SPEC §15.2): Chipchamp as both MCP server and client."""
from .client import McpClient, load_mcp_tools
from .server import McpServer, exposed_tools

__all__ = ["McpServer", "McpClient", "exposed_tools", "load_mcp_tools"]
