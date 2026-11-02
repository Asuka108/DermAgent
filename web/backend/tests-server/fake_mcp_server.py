# -*- coding: utf-8 -*-
"""测试用 stdio MCP 服务器：一个只读工具 + 一个写工具。"""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("fake-mcp")


@server.tool(name="echo_read", description="只读回显输入文本",
             annotations=ToolAnnotations(readOnlyHint=True))
def echo_read(text: str) -> str:
    return "echo:" + str(text)


@server.tool(name="write_thing", description="写入数据（会被只读门禁拦截）",
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def write_thing(text: str) -> str:
    return "written:" + str(text)


if __name__ == "__main__":
    import anyio

    anyio.run(server.run_stdio_async)
