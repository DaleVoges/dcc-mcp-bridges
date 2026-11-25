"""Launches the MCP server over stdio (as Claude does) and calls tools through the protocol.

    python tests/test_mcp_protocol.py                          # server from this checkout
    python tests/test_mcp_protocol.py uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges blender-mcp-bridge
"""
import asyncio
import os
import sys

# Tests only use ports 7690-7691, so they can never reach a real Blender on 7677+.
os.environ["BLENDER_MCP_PORT"] = "7690"
os.environ["BLENDER_MCP_PORT_RANGE"] = "2"

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SRC = os.pathsep.join([os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core")])
COMMAND = sys.argv[1:] or [sys.executable, "-m", "blender_mcp_bridge"]


async def main():
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:],
                                   env=dict(os.environ, PYTHONPATH=SRC))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await session.call_tool("blender_status", {})
            print(res.content[0].text)
            res = await session.call_tool(
                "execute_python", {"code": "o = D.objects.new('viaMcp', None); C.scene.collection.objects.link(o); o"})
            print(res.content[0].text)
            await session.call_tool(
                "execute_python", {"code": "D.objects.remove(D.objects['viaMcp'])", "undoable": False})
            res = await session.call_tool("execute_python", {"code": "1/0"})
            is_error = getattr(res, "is_error", None)
            if is_error is None:
                is_error = res.isError
            print("is_error:", is_error, "|", res.content[0].text.strip().splitlines()[-1])
            assert is_error
            print("PROTOCOL OK")


asyncio.run(main())
