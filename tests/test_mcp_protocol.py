"""Launches the MCP server over stdio (as Claude does) and calls tools through the protocol.

    python tests/test_mcp_protocol.py                          # server from src/
    python tests/test_mcp_protocol.py uvx --from dist/x.whl maya-mcp-bridge   # any command
"""
import asyncio
import os
import sys

# Tests only use ports 7390-7391, so they can never reach (or wipe) a real Maya on 7377+.
os.environ["MAYA_MCP_PORT"] = "7390"
os.environ["MAYA_MCP_PORT_RANGE"] = "2"

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
COMMAND = sys.argv[1:] or [sys.executable, "-m", "maya_mcp_bridge"]


async def main():
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:],
                                   env=dict(os.environ, PYTHONPATH=SRC))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await session.call_tool("maya_status", {})
            print(res.content[0].text)
            res = await session.call_tool(
                "execute_python", {"code": "cmds.polyTorus(name='viaMcp')[0]"})
            print(res.content[0].text)
            res = await session.call_tool("execute_python", {"code": "1/0"})
            is_error = getattr(res, "is_error", None)
            if is_error is None:
                is_error = res.isError
            print("is_error:", is_error, "|", res.content[0].text.strip().splitlines()[-1])
            print("PROTOCOL OK")


asyncio.run(main())
