"""Launches the MCP server over stdio (as Claude does) and calls tools through the protocol.

    python tests/test_mcp_protocol.py                          # server from this checkout
    python tests/test_mcp_protocol.py uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges houdini-mcp-bridge
"""
import asyncio
import os
import sys

# Tests only use ports 7490-7491, so they can never reach a real Houdini on 7477+.
os.environ["HOUDINI_MCP_PORT"] = "7490"
os.environ["HOUDINI_MCP_PORT_RANGE"] = "2"

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SRC = os.pathsep.join([os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core")])
COMMAND = sys.argv[1:] or [sys.executable, "-m", "houdini_mcp_bridge"]


async def main():
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:],
                                   env=dict(os.environ, PYTHONPATH=SRC))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await session.call_tool("houdini_status", {})
            print(res.content[0].text)
            res = await session.call_tool(
                "execute_python", {"code": "hou.node('/obj').createNode('null', 'viaMcp')"})
            print(res.content[0].text)
            await session.call_tool(
                "execute_python", {"code": "hou.node('/obj/viaMcp').destroy()", "undoable": False})
            res = await session.call_tool("execute_python", {"code": "1/0"})
            is_error = getattr(res, "is_error", None)
            if is_error is None:
                is_error = res.isError
            print("is_error:", is_error, "|", res.content[0].text.strip().splitlines()[-1])
            assert is_error
            print("PROTOCOL OK")


asyncio.run(main())
