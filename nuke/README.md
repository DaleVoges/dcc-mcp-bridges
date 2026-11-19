# Nuke MCP Bridge

Let Claude control a live Foundry Nuke session with Python and TCL. Ask in plain language ("grade the plate warmer and add a soft vignette", "why is this Read node erroring?") and Claude inspects your script, builds and wires nodes, checks for errors, and can render a frame to see the result.

- Tested with Nuke 12.2 (Python 2.7) and Nuke 16.0 (Python 3.11) on Windows; macOS and Linux supported
- One install covers every Nuke version
- Every Claude action is a single undo step, so Ctrl+Z reverts it
- Handles several open Nukes safely: it asks which one you mean

```
Claude ──MCP──> nuke-mcp-bridge server ──localhost──> claude_mcp_bridge (inside Nuke)
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

### 1. Nuke: install the plug-in

```bash
uvx --from git+https://github.com/DaleVoges/nuke-mcp-bridge nuke-mcp-bridge install
```

This copies a `nuke_mcp_bridge` folder into your `~/.nuke` and adds a marked block to `~/.nuke/init.py` that loads it. Nothing else in `init.py` changes. Restart Nuke: the bridge starts with it, the Script Editor shows `[Claude MCP] Bridge listening on 127.0.0.1:7577`, and **Edit > Claude MCP** has Start/Stop items.

It only starts in graphical Nuke, never in terminal mode (`nuke -t`) or render jobs.

To **update**, run the same command again. To **uninstall**, run it with `uninstall` instead of `install`; that removes the folder and the block in `init.py`.

### 2. Claude: connect the server

**Claude Code:**

```bash
claude mcp add nuke -- uvx --from git+https://github.com/DaleVoges/nuke-mcp-bridge nuke-mcp-bridge
```

**Claude Desktop:** open Settings → Developer → Edit Config and add:

```json
{
  "mcpServers": {
    "nuke": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/DaleVoges/nuke-mcp-bridge", "nuke-mcp-bridge"]
    }
  }
}
```

Restart Claude Desktop afterwards. If it says it can't find `uvx`, replace `"uvx"` with the full path, for example `C:\\Users\\<you>\\.local\\bin\\uvx.exe` on Windows or `/Users/<you>/.local/bin/uvx` on macOS (`where uvx` / `which uvx` prints it).

Then start a new Claude session with Nuke open and ask *"check the Nuke connection"*.

## Tools Claude gets

| Tool | What it does |
|---|---|
| `execute_python` | Runs Python in Nuke with `nuke` and `nukescripts` imported. Returns the last expression (or `result`) and printed output |
| `execute_tcl` | Runs a TCL command or expression |
| `get_script_info` | Script name, format, fps, frame range, colour management, selection, node counts, Reads, Writes and Viewers |
| `list_nodes` | Nodes in the script or a Group, filtered by class and name |
| `inspect_node` | Class, inputs, dependents, changed knobs and expressions, error state, format, bbox, channels, frame range |
| `get_node_class_info` | A node class's knobs (names, types, labels, defaults, menu items) |
| `get_nuke_help` | Python docs for any `nuke` function or class |
| `find_node_errors` | Every node in an error state, such as a Read with a missing file |
| `render_frame` | Renders one frame of a node (default: what the Viewer shows) so Claude can see it. Uses temporary nodes, so nothing in the script changes |
| `nuke_status` | The selected Nuke: version, product, Python version, port, pid, script |
| `list_nuke_sessions` | Every running Nuke with the bridge started |
| `select_nuke_session` | Picks which Nuke the other tools talk to |

Variables persist between `execute_python` calls.

## Several Nukes at once

Each Nuke takes the first free port from 7577 to 7586, and the Script Editor shows which one it got.

- **One Nuke open:** Claude uses it automatically.
- **Several open:** the tools refuse to run until one is chosen. Claude lists them by version and open script, asks you which one, then selects it.
- **The selected Nuke closes:** the next call fails with "nothing was run". Each Nuke is identified by its process ID as well as its port, so if another Nuke later takes the same port, Claude won't send code to it by mistake.

Works alongside [maya-mcp-bridge](https://github.com/DaleVoges/maya-mcp-bridge) (ports 7377+) and [houdini-mcp-bridge](https://github.com/DaleVoges/houdini-mcp-bridge) (ports 7477+), so all three can be connected at once.

## Settings (environment variables)

| Var | Default | Used by |
|---|---|---|
| `NUKE_MCP_PORT` | `7577` | both: first port the bridge tries and the server scans |
| `NUKE_MCP_PORT_RANGE` | `10` | both: how many ports from there |
| `NUKE_MCP_AUTOSTART` | `1` | Nuke: set to `0` to not start the bridge with Nuke |
| `NUKE_MCP_HOST` | `127.0.0.1` | server |
| `NUKE_MCP_TIMEOUT` | `600` | server, seconds to wait for one Nuke call |

## Security

The bridge runs any code it receives with your user's permissions. It only listens on `127.0.0.1`, so other machines can't reach it, but any program on your computer can while it's running. Stop it from **Edit > Claude MCP > Stop Bridge**, or set `NUKE_MCP_AUTOSTART=0`, when you're not using it.

## Troubleshooting

- **"No Nuke found"**: check the Script Editor for `[Claude MCP] Bridge listening on ...`. If it's missing, use **Edit > Claude MCP > Start Bridge**; if that menu is missing too, rerun the install command and restart Nuke.
- **Claude says the bridge and server versions differ**: rerun the install command and restart Nuke.
- **A different .nuke folder**: Nuke uses `$HOME/.nuke` (on Windows `%HOME%` if set, else your user folder). Pass `--nuke-dir <path>` to install somewhere else.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . uv
```

Claude Code sessions opened in this folder use `.mcp.json`, which runs the server from `src/` (Windows venv path; on macOS/Linux use `.venv/bin/python`).

Layout:

- `src/nuke_mcp_bridge/nuke_plugin/`: the plug-in folder copied into `~/.nuke` (`claude_mcp_bridge.py`, `menu.py`); Python 2.7 and 3
- `src/nuke_mcp_bridge/install.py`: `install` / `uninstall`
- `src/nuke_mcp_bridge/server.py`: the MCP server

### Tests

The tests use ports 7590-7591 and a throwaway `HOME`, so they never touch a Nuke you have open or your real `.nuke` folder. Terminal mode needs a Nuke render license; the GUI test needs an interactive one. If your Nuke gets its license from a server, set `foundry_LICENSE` (e.g. `4101@your-license-server`) in the shell that runs the tests. Start a test bridge:

```powershell
& "C:\Program Files\Nuke16.0v8\Nuke16.0.exe" -t tests\run_bridge_nuke.py
```

Then:

```powershell
.\.venv\Scripts\python.exe tests\test_tools.py         # each tool, called directly
.\.venv\Scripts\python.exe tests\test_mcp_protocol.py  # through the MCP protocol, like Claude
```

These start their own Nukes (stop any test bridge first):

```powershell
.\.venv\Scripts\python.exe tests\test_multi_session.py "C:\Program Files\Nuke16.0v8\Nuke16.0.exe"
.\.venv\Scripts\python.exe tests\test_installer.py "C:\Program Files\Nuke16.0v8\Nuke16.0.exe"  # any number of Nukes
.\.venv\Scripts\python.exe tests\test_gui.py "C:\Program Files\Nuke12.2v2\Nuke12.2.exe" $env:TEMP  # opens a Nuke window
```

### Releasing

Bump `BRIDGE_VERSION` in `claude_mcp_bridge.py` and `__version__` in `src/nuke_mcp_bridge/__init__.py` together, then push to `main`. uvx installs from the repo, so that's the release. Users get the new server when Claude restarts, and update the Nuke side by rerunning the install command (`nuke_status` tells them when it's out of date).
