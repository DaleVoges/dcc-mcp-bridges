# Claude ↔ Maya MCP

Lets Claude control a live Autodesk Maya session with `maya.cmds`, PyMEL, OpenMaya or MEL.

```
Claude ──stdio/MCP──> server/maya_mcp_server.py ──TCP 127.0.0.1:7377──> claude_mcp_bridge.py (inside Maya)
```

- **`maya/plug-ins/claude_mcp_bridge.py`**: Maya plug-in. Starts a localhost-only JSON socket server and runs every request on Maya's main thread, inside one undo chunk. Works on Python 2.7 (Maya 2018 to 2020) and Python 3 (2022+).
- **`server/maya_mcp_server.py`**: the MCP server Claude launches. It runs outside Maya, in this folder's `.venv`.

## Install

```powershell
.\install.ps1
```

This creates `.venv`, writes `Documents\maya\modules\claude_mcp.mod` so every Maya version can see the plug-in, and registers the server with Claude Code if the `claude` CLI is on your PATH. Claude Code sessions opened in this folder also pick it up from `.mcp.json`. That file uses the Windows venv path; on macOS or Linux change it to `.venv/bin/python`.

For **Claude Desktop** (chat app), add this to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "maya": {
      "command": "C:/path/to/maya-mcp-bridge/.venv/Scripts/python.exe",
      "args": ["C:/path/to/maya-mcp-bridge/server/maya_mcp_server.py"]
    }
  }
}
```

In Maya, open **Windows > Settings/Preferences > Plug-in Manager**, load `claude_mcp_bridge.py` and tick *Auto load*. The Script Editor should print `[Claude MCP] Bridge listening on 127.0.0.1:7377`.

Or from the Script Editor:

```python
import maya.cmds as cmds
cmds.loadPlugin("claude_mcp_bridge.py")
```

## Tools

| Tool | What it does |
|---|---|
| `maya_status` | The selected Maya: version, Python version, port, pid, open scene |
| `list_maya_sessions` | Every running Maya with the plug-in loaded |
| `select_maya_session` | Picks which Maya the other tools talk to |
| `execute_python` | Runs Python in Maya. Returns the last expression (or `result`) and printed output |
| `execute_mel` | Runs MEL |
| `get_scene_info` | File, units, up axis, frame range, selection, node counts |
| `list_nodes` | `cmds.ls` with type, wildcard and selection filters |
| `inspect_node` | Type, hierarchy, matrix, attributes and connections of one node |
| `get_command_help` | Docs for cmds, MEL, OpenMaya or PyMEL commands and classes |
| `capture_viewport` | Screenshot of the viewport, so Claude can see the result |

`execute_python` has these names already imported: `cmds`/`mc`, `mel`, `om`/`om2` (`maya.api.OpenMaya`), `om1` (`maya.OpenMaya`), `oma`, and `pm` (PyMEL, imported the first time you use it). Variables persist between calls.

## Several Mayas at once

Each Maya takes the first free port from 7377 to 7386, and the Script Editor shows which one it got (`Bridge listening on 127.0.0.1:7378 (pid 1234)`).

- **One Maya open:** Claude uses it automatically.
- **Several open:** the tools refuse to run until one is chosen. Claude lists them by version, port and open scene, asks you which one, then selects it.
- **The selected Maya closes:** the next call fails with "nothing was run". Each Maya is identified by its process ID as well as its port, so if another Maya later takes the same port, Claude won't send code to it by mistake.
- `maya_status` and `list_maya_sessions` answer even while Maya is busy running a long command.

## Settings (environment variables)

| Var | Default | Used by |
|---|---|---|
| `MAYA_MCP_PORT` | `7377` | both: first port the plug-in tries and the server scans |
| `MAYA_MCP_PORT_RANGE` | `10` | both: how many ports from there |
| `MAYA_MCP_HOST` | `127.0.0.1` | server |
| `MAYA_MCP_TIMEOUT` | `600` | server, seconds to wait for one Maya call |

## Security

The bridge runs any code it receives with your user's permissions. It only listens on `127.0.0.1`, but any local process can connect while the plug-in is loaded. Unload it when you're not using it.

## Testing

Start a bridge, either in the Maya GUI (load the plug-in) or headless:

```powershell
& "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe" tests\run_bridge_mayapy.py
```

Then, in another terminal:

```powershell
.\.venv\Scripts\python.exe tests\test_tools.py         # calls each tool function directly
.\.venv\Scripts\python.exe tests\test_mcp_protocol.py  # launches the server over stdio, like Claude does
```

Multi-Maya tests (they kill test Mayas, so only run them against headless `mayapy` bridges, never your GUI Maya):

```powershell
# with two run_bridge_mayapy.py bridges running:
.\.venv\Scripts\python.exe tests\test_multi_session.py
# with one running:
.\.venv\Scripts\python.exe tests\test_port_takeover.py "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe"
```

`capture_viewport` needs the GUI, because there is no viewport in batch mode.

Headless/mayapy note: Maya commands must run on the main thread, so after `claude_mcp_bridge.start()` call `claude_mcp_bridge.serve_batch()` from the main thread (see `tests/run_bridge_mayapy.py`). In the GUI the plug-in does this for you.
