"""Launches the MCP server over stdio (as Claude does) and calls tools through the protocol.

    python tests/test_mcp_protocol.py                          # server from src/
    python tests/test_mcp_protocol.py uvx --from git+https://github.com/DaleVoges/nuke-mcp-bridge nuke-mcp-bridge
"""
import asyncio
import os
import sys

# Tests only use ports 7590-7591, so they can never reach a real Nuke on 7577+.
os.environ["NUKE_MCP_PORT"] = "7590"
os.environ["NUKE_MCP_PORT_RANGE"] = "2"

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
COMMAND = sys.argv[1:] or [sys.executable, "-m", "nuke_mcp_bridge"]


async def main():
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:],
                                   env=dict(os.environ, PYTHONPATH=SRC))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await session.call_tool("nuke_status", {})
            print(res.content[0].text)
            res = await session.call_tool(
                "execute_python", {"code": "nuke.nodes.NoOp(name='viaMcp')"})
            print(res.content[0].text)
            await session.call_tool(
                "execute_python", {"code": "nuke.delete(nuke.toNode('viaMcp'))", "undoable": False})
            res = await session.call_tool("execute_python", {"code": "1/0"})
            is_error = getattr(res, "is_error", None)
            if is_error is None:
                is_error = res.isError
            print("is_error:", is_error, "|", res.content[0].text.strip().splitlines()[-1])
            assert is_error
            print("PROTOCOL OK")


asyncio.run(main())
