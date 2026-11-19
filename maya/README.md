# Maya MCP Bridge

Let Claude control a live Autodesk Maya session with `maya.cmds`, OpenMaya, PyMEL or MEL. Ask in plain language ("make a 5×5 grid of cubes with random heights", "why is this mesh's skinning broken?") and Claude inspects your scene, runs the code, and can screenshot the viewport to check the result.

- Works with Maya 2018 to 2027 on Windows, macOS and Linux
- Every Claude action is a single undo step, so Ctrl+Z reverts it
- Handles several open Mayas safely: it asks which one you mean

```
Claude ──MCP──> maya-mcp-bridge server ──localhost──> claude_mcp_bridge plug-in (inside Maya)
```

## Install

Installation has two parts: the plug-in goes into Maya, and the server gets connected to Claude. It takes about two minutes.

### 1. Maya: drag and drop

1. Download `maya-mcp-bridge-<version>.zip` from the [latest release](https://github.com/DaleVoges/maya-mcp-bridge/releases/latest) and unzip it anywhere.
2. Drag **`drag_to_install.py`** into a Maya viewport.

That's it. The plug-in is copied into your Maya prefs, loads right away, and loads automatically every time Maya starts. One install covers every Maya version you have. You can delete the download afterwards.

To **update**, drag the new version's `drag_to_install.py` in and click *Update*. To **uninstall**, drag it in again and click *Uninstall*.

### 2. Claude: connect the server

The server runs with [uv](https://docs.astral.sh/uv/getting-started/installation/), which downloads it from this repo and runs it for you, with no manual Python setup. You need [git](https://git-scm.com/downloads) installed (uv uses it to fetch the repo). Install uv once:

```powershell
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### Connect Claude

**Claude Code:**

```bash
claude mcp add maya -- uvx --from git+https://github.com/DaleVoges/maya-mcp-bridge maya-mcp-bridge
```

**Claude Desktop:** open Settings → Developer → Edit Config and add:

```json
{
  "mcpServers": {
    "maya": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DaleVoges/maya-mcp-bridge", "maya-mcp-bridge"]
    }
  }
}
```

Restart Claude Desktop afterwards. If it says it can't find `uvx`, replace `"uvx"` with the full path, for example `C:\\Users\\<you>\\.local\\bin\\uvx.exe` on Windows or `/Users/<you>/.local/bin/uvx` on macOS (`where uvx` / `which uvx` prints it).

Then start a new Claude session with Maya open and ask *"check the Maya connection"*.

## Tools Claude gets

| Tool | What it does |
|---|---|
| `execute_python` | Runs Python in Maya. Returns the last expression (or `result`) and printed output |
| `execute_mel` | Runs MEL |
| `get_scene_info` | File, units, up axis, frame range, selection, node counts |
| `list_nodes` | `cmds.ls` with type, wildcard and selection filters |
| `inspect_node` | Type, hierarchy, matrix, attributes and connections of one node |
| `get_command_help` | Docs for cmds, MEL, OpenMaya or PyMEL commands and classes |
| `capture_viewport` | Screenshot of the viewport, so Claude can see the result |
| `maya_status` | The selected Maya: version, Python version, port, pid, open scene |
| `list_maya_sessions` | Every running Maya with the plug-in loaded |
| `select_maya_session` | Picks which Maya the other tools talk to |

`execute_python` has these names already imported: `cmds`/`mc`, `mel`, `om`/`om2` (`maya.api.OpenMaya`), `om1` (`maya.OpenMaya`), `oma`, and `pm` (PyMEL, imported the first time it's used; PyMEL is an optional install in Maya 2022+). Variables persist between calls.

## Several Mayas at once

Each Maya takes the first free port from 7377 to 7386, and the Script Editor shows which one it got (`Bridge listening on 127.0.0.1:7378 (pid 1234)`).

- **One Maya open:** Claude uses it automatically.
- **Several open:** the tools refuse to run until one is chosen. Claude lists them by version and open scene, asks you which one, then selects it.
- **The selected Maya closes:** the next call fails with "nothing was run". Each Maya is identified by its process ID as well as its port, so if another Maya later takes the same port, Claude won't send code to it by mistake.

## Settings (environment variables)

| Var | Default | Used by |
|---|---|---|
| `MAYA_MCP_PORT` | `7377` | both: first port the plug-in tries and the server scans |
| `MAYA_MCP_PORT_RANGE` | `10` | both: how many ports from there |
| `MAYA_MCP_HOST` | `127.0.0.1` | server |
| `MAYA_MCP_TIMEOUT` | `600` | server, seconds to wait for one Maya call |

## Security

The plug-in runs any code it receives with your user's permissions. It only listens on `127.0.0.1`, so other machines can't reach it, but any program on your computer can while it's loaded. Unload it from the Plug-in Manager when you're not using it.

## Troubleshooting

- **"No Maya found"**: check the Script Editor for `[Claude MCP] Bridge listening on ...`. If it's missing, load `claude_mcp_bridge.py` in **Windows > Settings/Preferences > Plug-in Manager**.
- **Claude says the plug-in and server versions differ**: drag the latest `drag_to_install.py` into Maya.
- **Load the plug-in from a script**: `cmds.loadPlugin("claude_mcp_bridge.py")` (Python plug-ins load by file name, including `.py`).

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . uv
```

Claude Code sessions opened in this folder use `.mcp.json`, which runs the server from `src/` (Windows venv path; on macOS/Linux use `.venv/bin/python`).

Layout:

- `maya_module/plug-ins/claude_mcp_bridge.py`: the Maya plug-in (Python 2.7 and 3)
- `drag_to_install.py`: the drag-and-drop installer
- `src/maya_mcp_bridge/`: the MCP server package (uvx installs it from this repo)
- `tools/build_release.py`: builds the Maya release zip into `dist/`

### Tests

The tests use ports 7390-7391 and headless `mayapy`, so they never touch a Maya you have open. Start a test bridge:

```powershell
& "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe" tests\run_bridge_mayapy.py
```

Then:

```powershell
.\.venv\Scripts\python.exe tests\test_tools.py         # each tool, called directly
.\.venv\Scripts\python.exe tests\test_mcp_protocol.py  # through the MCP protocol, like Claude
```

Multi-Maya tests (they kill the headless test Mayas):

```powershell
# with two run_bridge_mayapy.py bridges running:
.\.venv\Scripts\python.exe tests\test_multi_session.py
# with one running:
.\.venv\Scripts\python.exe tests\test_port_takeover.py "C:\Program Files\Autodesk\Maya2026\bin\mayapy.exe"
```

Installer test, against a throwaway prefs folder (set `MAYA_APP_DIR` to an empty temp folder first, then unzip a built release):

```powershell
mayapy tests\test_installer.py <unzipped release folder> install
mayapy tests\test_installer.py <unzipped release folder> check-module   # in a fresh mayapy
```

`capture_viewport` needs the Maya GUI. Headless Maya has no viewport.

### Releasing

1. Bump `PLUGIN_VERSION` in the plug-in and `__version__` in `src/maya_mcp_bridge/__init__.py` (the build fails if they differ).
2. Push to `main`. That's the server release: uvx installs from the repo.
3. `python tools/build_release.py`, then create a GitHub release tagged `v<version>` and attach `dist/maya-mcp-bridge-<version>.zip`.
