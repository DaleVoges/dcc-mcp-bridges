"""Runs the bridge in background Blender for tests:

    blender --background --factory-startup --python tests/run_bridge_blender.py

Uses port 7690 (BLENDER_MCP_PORT, set below if not already set), away from real Blenders.
"""
import os
import sys

os.environ.setdefault("BLENDER_MCP_PORT", "7690")
os.environ.setdefault("BLENDER_MCP_PORT_RANGE", "2")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "blender_mcp_bridge", "blender_addon"))

import claude_mcp_bridge as bridge  # noqa: E402

bridge.start()
print("READY", flush=True)
bridge.serve_batch()
