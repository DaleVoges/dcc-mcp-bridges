# Blender MCP Bridge

Let Claude control a live Blender session with `bpy` Python. Ask in plain language ("scatter 200 rocks on this plane", "why does this geometry nodes setup output nothing?", "give everything in the Props collection a bevel modifier") and Claude inspects your scene, objects and node trees, makes the changes, and can look at the viewport to check the result.

- Tested with Blender 4.5 LTS on Windows (Blender 4.2+ should work); macOS and Linux supported
- Every Claude action is a single undo step, so Ctrl+Z reverts it
- Handles several open Blenders safely: it asks which one you mean

```
Claude ──MCP──> blender-mcp-bridge server ──localhost──> claude_mcp_bridge (inside Blender)
```

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and [git](https://git-scm.com/downloads). uv downloads and runs everything from this repo, with no manual Python setup. Install uv once:

```powershell
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. Blender: install the startup script

```bash
uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges blender-mcp-bridge install
```

This copies `claude_mcp_bridge.py` into `scripts/startup` of every Blender version you've run (each version folder under `%APPDATA%\Blender Foundation\Blender` on Windows, `~/Library/Application Support/Blender` on macOS, `~/.config/blender` on Linux). Blender loads startup scripts by itself, so there is no add-on to enable. Restart Blender: the bridge starts with it, and the system console (Window > Toggle System Console on Windows) shows `[Claude MCP] Bridge listening on 127.0.0.1:7677`.

It only starts in graphical Blender, never with `--background`, so command-line renders and render farm jobs aren't affected.

To **update**, run the same command again. To **uninstall**, run it with `uninstall` instead of `install`.

### 2. Claude: connect the server

**Claude Code:**

```bash
claude mcp add blender -- uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges blender-mcp-bridge
```

**Claude Desktop:** open Settings → Developer → Edit Config and add:

```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DaleVoges/dcc-mcp-bridges", "blender-mcp-bridge"]
    }
  }
}
```

Restart Claude Desktop afterwards. If it says it can't find `uvx`, replace `"uvx"` with the full path, for example `C:\\Users\\<you>\\.local\\bin\\uvx.exe` on Windows or `/Users/<you>/.local/bin/uvx` on macOS (`where uvx` / `which uvx` prints it).

Then start a new Claude session with Blender open and ask *"check the Blender connection"*.

## Tools Claude gets

| Tool | What it does |
|---|---|
| `execute_python` | Runs Python in Blender with `bpy`, `bmesh`, `mathutils`, `C` (context) and `D` (data) imported. Returns the last expression (or `result`) and printed output |
| `get_scene_info` | .blend file, scenes, frame range, fps, render engine and resolution, objects per type, collections, selection |
| `list_objects` | Objects in the scene, filtered by name, type, collection or selection |
| `inspect_object` | Transforms, parent/children, modifiers and constraints with their settings, materials, animation, and mesh counts, attributes, UV maps and bounds |
| `inspect_node_tree` | A material, geometry nodes group, world or compositor: nodes, changed input values, links and the group interface |
| `get_bpy_help` | Properties of any `bpy.types` class or arguments of any operator (names, types, defaults, enum items), or a search by name |
| `capture_viewport` | Picture of the 3D View, optionally through a camera |
| `blender_status` | The selected Blender: version, Python, port, pid, .blend file |
| `list_blender_sessions` | Every running Blender with the bridge started |
| `select_blender_session` | Picks which Blender the other tools talk to |

Variables persist between `execute_python` calls. Operators (`bpy.ops`) that need a 3D View can be run inside `with bpy.context.temp_override(**view3d_override()):`, a helper the bridge provides.

## Several Blenders at once

Each Blender takes the first free port from 7677 to 7686, and the system console shows which one it got.

- **One Blender open:** Claude uses it automatically.
- **Several open:** the tools refuse to run until one is chosen. Claude lists them by version and open .blend file, asks you which one, then selects it.
- **The selected Blender closes:** the next call fails with "nothing was run". Each Blender is identified by its process ID as well as its port, so if another Blender later takes the same port, Claude won't send code to it by mistake.

All the bridges ([Maya](../maya), [Houdini](../houdini), [Nuke](../nuke), [Blender](../blender)) use their own ports (7377+, 7477+, 7577+, 7677+), so they can all be connected at once.

## Settings (environment variables)

| Var | Default | Used by |
|---|---|---|
| `BLENDER_MCP_PORT` | `7677` | both: first port the bridge tries and the server scans |
| `BLENDER_MCP_PORT_RANGE` | `10` | both: how many ports from there |
| `BLENDER_MCP_AUTOSTART` | `1` | Blender: set to `0` to not start the bridge with Blender |
| `BLENDER_MCP_HOST` | `127.0.0.1` | server |
| `BLENDER_MCP_TIMEOUT` | `600` | server, seconds to wait for one Blender call |

## Security

The bridge runs any code it receives with your user's permissions. It only listens on `127.0.0.1`, so other machines can't reach it, but any program on your computer can while it's running. Stop it from the Python Console (`import claude_mcp_bridge; claude_mcp_bridge.stop()`) or set `BLENDER_MCP_AUTOSTART=0` when you're not using it.

## Troubleshooting

- **"No Blender found"**: check the system console for `[Claude MCP] Bridge listening on ...`. If it's missing, rerun the install command and restart Blender. A Blender version you have never opened has no config folder yet: open it once, then rerun the install.
- **Start or stop it by hand**: in the Python Console, `import claude_mcp_bridge; claude_mcp_bridge.start()` (or `.stop()`).
- **Claude says the bridge and server versions differ**: rerun the install command and restart Blender.
- **Custom or portable config location**: pass `--config-dir <path to the version folder, e.g. .../Blender/4.5>` to install there. If you set `BLENDER_USER_SCRIPTS`, copy `claude_mcp_bridge.py` into its `startup` folder instead.

## Development

Set up the venv at the repo root as the [main README](../README.md#development) describes, and run the commands below from the repo root.

Layout:

- `blender/blender_mcp_bridge/blender_addon/claude_mcp_bridge.py`: the bridge that runs inside Blender
- `blender/blender_mcp_bridge/install.py`: copies it into Blender's startup folders (`install` / `uninstall`)
- `blender/blender_mcp_bridge/server.py`: the MCP server

### Tests

The tests use ports 7690-7691 and throwaway config folders, so they never touch a Blender you have open or your real config. Start a test bridge in background Blender:

```powershell
& "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe" --background --factory-startup --python blender\tests\run_bridge_blender.py
```

Then:

```powershell
.\.venv\Scripts\python.exe blender\tests\test_tools.py         # each tool, called directly
.\.venv\Scripts\python.exe blender\tests\test_mcp_protocol.py  # through the MCP protocol, like Claude
```

These start their own Blenders (stop any test bridge first):

```powershell
$B = "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
.\.venv\Scripts\python.exe blender\tests\test_multi_session.py $B
.\.venv\Scripts\python.exe blender\tests\test_installer.py $B   # any number of blender.exe
.\.venv\Scripts\python.exe blender\tests\test_gui.py $B         # opens a Blender window
```

### Releasing

Bump `BRIDGE_VERSION` in `claude_mcp_bridge.py` and `__version__` in `blender/blender_mcp_bridge/__init__.py` together, then push to `main`. uvx installs from the repo, so that's the release. Users get the new server when Claude restarts, and update the Blender side by rerunning the install command (`blender_status` tells them when it's out of date).
