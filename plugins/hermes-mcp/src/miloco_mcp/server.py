"""Miloco MCP Server — Entry Point.

Wraps the Miloco backend REST API into MCP tools for use with Hermes Agent.

Usage:
    python -m miloco_mcp.server              # stdio mode (default, for Hermes)
    python -m miloco_mcp.server --http       # HTTP mode
    python -m miloco_mcp.server --http --port 8001
"""

import sys

from fastmcp import FastMCP

from .tools import register_all_tools


def create_server() -> FastMCP:
    """Create and configure the Miloco MCP server."""
    server = FastMCP(
        name="miloco",
        instructions=(
            "Xiaomi Miloco 全屋智能 MCP Server。提供米家设备查询与控制、"
            "摄像头实时感知、任务与规则管理、家庭成员档案等功能。\n\n"
            "使用前请确认 Miloco 后端已启动 (miloco-cli service start)。\n"
            "默认连接 http://127.0.0.1:1810，可通过 MILOCO_BASE_URL 环境变量修改。"
        ),
    )
    register_all_tools(server)
    return server


if __name__ == "__main__":
    use_http = "--http" in sys.argv
    port = 8001
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    server = create_server()
    if use_http:
        server.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        server.run(transport="stdio")
