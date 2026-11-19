# Houdini MCP Bridge

Let Claude control a live SideFX Houdini session with `hou` Python and HScript. Ask in plain language ("scatter 500 spheres on this grid and randomise their scale", "why is this wrangle erroring?") and Claude inspects your networks, builds and wires nodes, checks for cook errors, and can screenshot the viewport to check the result.

- Tested with Houdini 20.5 and 21.0 (Houdini 19.5+ should work) on Windows; macOS and Linux supported
- Every Claude action is a single undo step, so Ctrl+Z reverts it
- Handles several open Houdinis safely: it asks which one you mean

```
Claude ──MCP──> houdini-mcp-bridge server ──localhost──> claude_mcp_bridge (inside Houdini)
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

### 1. Houdini: install the package

```bash
uvx --from git+https://github.com/DaleVoges/houdini-mcp-bridge houdini-mcp-bridge install
```

This adds a [Houdini package](https://www.sidefx.com/docs/houdini/ref/plugins.html) to every Houdini version you've run (each `houdiniX.Y` prefs folder). Restart Houdini: the bridge starts with it, and the console shows `[Claude MCP] Bridge listening on 127.0.0.1:7477`.

It only starts in graphical Houdini, never in hython, so batch renders and PDG workers aren't affected.

To **update**, run the same command again. To **uninstall**, run it with `uninstall` instead of `install`.

### 2. Claude: connect the server

**Claude Code:**

```bash
claude mcp add houdini -- uvx --from git+https://github.com/DaleVoges/houdini-mcp-bridge houdini-mcp-bridge
```

**Claude Desktop:** open Settings → Developer → Edit Config and add:

```json
{
  "mcpServers": {
    "houdini": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DaleVoges/houdini-mcp-bridge", "houdini-mcp-bridge"]
    }
  }
}
```

Restart Claude Desktop afterwards. If it says it can't find `uvx`, replace `"uvx"` with the full path, for example `C:\\Users\\<you>\\.local\\bin\\uvx.exe` on Windows or `/Users/<you>/.local/bin/uvx` on macOS (`where uvx` / `which uvx` prints it).

Then start a new Claude session with Houdini open and ask *"check the Houdini connection"*.

## Tools Claude gets

| Tool | What it does |
|---|---|
| `execute_python` | Runs Python in Houdini with `hou` imported. Returns the last expression (or `result`) and printed output |
| `execute_hscript` | Runs an HScript command |
| `get_scene_info` | .hip file, frame range, fps, selection, node counts per context |
| `list_nodes` | Nodes under a network, filtered by name and type |
| `inspect_node` | Type, flags, inputs/outputs, changed parms and expressions, errors, and SOP geometry (counts, attributes, groups, bounds) |
| `get_node_type_info` | A node type's exact name, inputs and parms (names, types, defaults, menus), or a search by wildcard |
| `get_hou_help` | Python docs for any `hou` class or function |
| `find_node_errors` | Every node under a path with cook errors or warnings |
| `capture_viewport` | Screenshot of the Scene View, optionally through a camera |
| `houdini_status` | The selected Houdini: version, product, license, port, pid, .hip file |
| `list_houdini_sessions` | Every running Houdini with the bridge started |
| `select_houdini_session` | Picks which Houdini the other tools talk to |

Variables persist between `execute_python` calls.

## Several Houdinis at once

Each Houdini takes the first free port from 7477 to 7486, and the console shows which one it got.

- **One Houdini open:** Claude uses it automatically.
- **Several open:** the tools refuse to run until one is chosen. Claude lists them by version and open .hip file, asks you which one, then selects it.
- **The selected Houdini closes:** the next call fails with "nothing was run". Each Houdini is identified by its process ID as well as its port, so if another Houdini later takes the same port, Claude won't send code to it by mistake.

Works alongside [maya-mcp-bridge](https://github.com/DaleVoges/maya-mcp-bridge): Maya uses ports 7377 and up, so both can be connected at once.

## Settings (environment variables)

| Var | Default | Used by |
|---|---|---|
| `HOUDINI_MCP_PORT` | `7477` | both: first port the bridge tries and the server scans |
| `HOUDINI_MCP_PORT_RANGE` | `10` | both: how many ports from there |
| `HOUDINI_MCP_AUTOSTART` | `1` | Houdini: set to `0` to not start the bridge with Houdini |
| `HOUDINI_MCP_HOST` | `127.0.0.1` | server |
| `HOUDINI_MCP_TIMEOUT` | `600` | server, seconds to wait for one Houdini call |

Set the Houdini-side ones in `houdini.env` or your environment.

## Security

The bridge runs any code it receives with your user's permissions. It only listens on `127.0.0.1`, so other machines can't reach it, but any program on your computer can while it's running. Stop it from the Python Shell (`import claude_mcp_bridge; claude_mcp_bridge.stop()`) or set `HOUDINI_MCP_AUTOSTART=0` when you're not using it.

## Troubleshooting

- **"No Houdini found"**: check the Houdini console (Windows > Shell) for `[Claude MCP] Bridge listening on ...`. If it's missing, rerun the install command and restart Houdini.
- **Start or stop it by hand**: in Windows > Python Shell, `import claude_mcp_bridge; claude_mcp_bridge.start()` (or `.stop()`).
- **Claude says the bridge and server versions differ**: rerun the install command and restart Houdini.
- **Custom prefs location**: the installer follows `HOUDINI_USER_PREF_DIR`. To target one folder, pass `--prefs-dir <path to houdiniX.Y>`.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . uv
```

Claude Code sessions opened in this folder use `.mcp.json`, which runs the server from `src/` (Windows venv path; on macOS/Linux use `.venv/bin/python`).

Layout:

- `src/houdini_mcp_bridge/houdini_package/scripts/python/claude_mcp_bridge.py`: the bridge that runs inside Houdini
- `src/houdini_mcp_bridge/install.py`: writes the Houdini package (`install` / `uninstall`)
- `src/houdini_mcp_bridge/server.py`: the MCP server

### Tests

The tests use ports 7490-7491 and throwaway prefs folders, so they never touch a Houdini you have open or your real prefs. Start a test bridge in headless hython:

```powershell
& "C:\Program Files\Side Effects Software\Houdini 21.0.700\bin\hython.exe" tests\run_bridge_hython.py
```

Then:

```powershell
.\.venv\Scripts\python.exe tests\test_tools.py         # each tool, called directly
.\.venv\Scripts\python.exe tests\test_mcp_protocol.py  # through the MCP protocol, like Claude
```

These start their own Houdinis (stop any test bridge first):

```powershell
$H = "C:\Program Files\Side Effects Software\Houdini 21.0.700\bin"
.\.venv\Scripts\python.exe tests\test_multi_session.py "$H\hython.exe"
.\.venv\Scripts\python.exe tests\test_installer.py "$H\hython.exe"      # any number of hythons
.\.venv\Scripts\python.exe tests\test_gui.py "$H\houdinicore.exe" $env:TEMP  # opens a Houdini window
```

### Releasing

Bump `BRIDGE_VERSION` in `claude_mcp_bridge.py` and `__version__` in `src/houdini_mcp_bridge/__init__.py` together, then push to `main`. uvx installs from the repo, so that's the release. Users get the new server when Claude restarts, and update the Houdini side by rerunning the install command (`houdini_status` tells them when it's out of date).
