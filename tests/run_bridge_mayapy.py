"""Runs the bridge inside headless Maya (mayapy) for testing.

    "C:\\Program Files\\Autodesk\\Maya2026\\bin\\mayapy.exe" tests\\run_bridge_mayapy.py
"""
import os
import sys

import maya.standalone

maya.standalone.initialize(name="python")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "maya", "plug-ins"))
import claude_mcp_bridge  # noqa: E402

claude_mcp_bridge.start()
print("READY", flush=True)
try:
    claude_mcp_bridge.serve_batch()  # runs Maya commands on this (main) thread
except KeyboardInterrupt:
    claude_mcp_bridge.stop()
