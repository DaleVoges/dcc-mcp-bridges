"""Runs the bridge in headless hython for tests:  hython tests/run_bridge_hython.py

Uses port 7490 (HOUDINI_MCP_PORT, set below if not already set), away from real Houdinis.
"""
import os
import sys

os.environ.setdefault("HOUDINI_MCP_PORT", "7490")
os.environ.setdefault("HOUDINI_MCP_PORT_RANGE", "2")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "houdini_mcp_bridge", "houdini_package",
                                "scripts", "python"))

import claude_mcp_bridge as bridge  # noqa: E402

bridge.start()
print("READY", flush=True)
bridge.serve_batch()
