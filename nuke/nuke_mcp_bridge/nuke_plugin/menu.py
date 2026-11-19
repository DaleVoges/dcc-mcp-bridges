# Installed by nuke-mcp-bridge. Runs in graphical Nuke only: starts the Claude MCP bridge
# and adds an Edit > Claude MCP menu to start/stop it.
import nuke

try:
    import claude_mcp_bridge

    claude_mcp_bridge.autostart()
    _menu = nuke.menu("Nuke").addMenu("Edit/Claude MCP")
    _menu.addCommand("Start Bridge", "import claude_mcp_bridge; claude_mcp_bridge.start()")
    _menu.addCommand("Stop Bridge", "import claude_mcp_bridge; claude_mcp_bridge.stop()")
except Exception as exc:
    print("[Claude MCP] Bridge not started: %s" % exc)
