"""Runs the bridge in Nuke terminal mode for tests:  Nuke16.0.exe -t tests/run_bridge_nuke.py

Uses port 7590 (NUKE_MCP_PORT, set below if not already set), away from real Nukes.
"""
from __future__ import print_function

import os
import sys

os.environ.setdefault("NUKE_MCP_PORT", "7590")
os.environ.setdefault("NUKE_MCP_PORT_RANGE", "2")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "nuke_mcp_bridge", "nuke_plugin"))

import claude_mcp_bridge as bridge  # noqa: E402

bridge.start()
print("READY")
sys.stdout.flush()
bridge.serve_batch()
