"""Miloco MCP Tools — Unified registration."""

from fastmcp import FastMCP

from .devices import mcp as devices_mcp
from .home import mcp as home_mcp
from .perception import mcp as perception_mcp
from .tasks import mcp as tasks_mcp


def register_all_tools(server: FastMCP) -> None:
    """Import and mount all tool modules onto the given FastMCP server."""
    for sub in [devices_mcp, perception_mcp, tasks_mcp, home_mcp]:
        server.mount(sub)
