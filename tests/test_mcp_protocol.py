"""Launches the MCP server over stdio (as Claude does) and calls tools through the protocol."""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "maya_mcp_server.py")


async def main():
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
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
