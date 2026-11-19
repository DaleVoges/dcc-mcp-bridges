"""Shared plumbing for the DCC MCP servers (Maya, Houdini, Nuke).

Each app's bridge runs inside the app and answers newline-delimited JSON on a local TCP
port. `Bridge` finds those ports, keeps the selected session's connection, and turns
replies into text for Claude. Each app's server.py defines its own tools on top of it.
"""
from .bridge import Bridge, BridgeError, FastMCP, Image, SessionGoneError, create_server

__version__ = "1.0.0"
__all__ = ["Bridge", "BridgeError", "FastMCP", "Image", "SessionGoneError", "create_server"]
