# DCC MCP Bridges

Let Claude drive live **Maya**, **Houdini** and **Nuke** sessions. Ask in plain language ("make a grid of cubes", "set up a fire sim on this box", "load these renders into the comp template") and Claude inspects the scene, runs the code inside the app, and can look at the viewport or a rendered frame to check the result.

| App | Folder | Runs code with | Install inside the app |
|---|---|---|---|
| Autodesk Maya 2018-2027 | [`maya/`](maya) | `maya.cmds`, OpenMaya, PyMEL, MEL | drag `drag_to_install.py` into Maya |
| SideFX Houdini 20.5+ | [`houdini/`](houdini) | `hou`, HScript | `uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges houdini-mcp-bridge install` |
| Foundry Nuke 12+ | [`nuke/`](nuke) | `nuke`, `nukescripts`, TCL | `uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges nuke-mcp-bridge install` |

Each app's README has the full install steps, the tool list and troubleshooting.

```
Claude ──MCP──> <app>-mcp-bridge server ──localhost──> claude_mcp_bridge (inside the app)
```

- Every Claude action is one undo step, so Ctrl+Z reverts it.
- Several copies of an app can be open: Claude asks which one you mean instead of guessing.
- Each app has its own port range (Maya 7377+, Houdini 7477+, Nuke 7577+), so all three can be connected at once.

## Connect Claude

The servers run with [uv](https://docs.astral.sh/uv/getting-started/installation/), which fetches this repo with [git](https://git-scm.com/downloads) and runs it, with no manual Python setup. Add whichever apps you use:

```bash
claude mcp add maya -- uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges maya-mcp-bridge
claude mcp add houdini -- uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges houdini-mcp-bridge
claude mcp add nuke -- uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges nuke-mcp-bridge
```

For Claude Desktop, see the app's README.

### Moving from the old single-app repos

The bridges used to live in `maya-mcp-bridge`, `houdini-mcp-bridge` and `nuke-mcp-bridge`. Those repos are archived and get no more updates. To switch, rerun the `claude mcp add` line above for each app (remove the old one first with `claude mcp remove <app>`). For Houdini and Nuke, also rerun the install command in the table so the in-app part matches. The Maya plug-in itself has not changed.

## Layout

```
core/dcc_mcp_core/   shared by all servers: finds sessions, keeps the connection, formats replies
maya/                Maya plug-in, drag-and-drop installer, server (maya_mcp_bridge), tests
houdini/             Houdini package, installer, server (houdini_mcp_bridge), tests
nuke/                Nuke plug-in, installer, server (nuke_mcp_bridge), tests
tools/               build_maya_release.py: builds the Maya download zip
```

One install of this repo provides all three servers (`maya-mcp-bridge`, `houdini-mcp-bridge`, `nuke-mcp-bridge`). Each app's server keeps its own tools; `core` holds what they share. Code that runs inside the apps stays self-contained (it has to run on each app's own Python, down to 2.7 for Maya 2020 and Nuke 12), so it does not use `core`.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . uv
```

Claude Code sessions opened in this folder use `.mcp.json`, which runs all three servers from this checkout (Windows venv path; on macOS/Linux use `.venv/bin/python`).

Core tests need no app installed:

```powershell
.\.venv\Scripts\python.exe core\tests\test_bridge.py
```

Each app's tests start headless copies of that app on test-only ports, so they never touch a session you have open. See the Tests section of each app's README.

### Adding another app

1. Make `<app>/<app>_mcp_bridge/` with a `server.py` that builds a `dcc_mcp_core.Bridge` and defines the app's tools (copy one of the existing servers).
2. Write the in-app bridge: it answers the same line-based JSON protocol (`ping`, `python`, `capture`...) on its own port range, and returns `"app": "<app>"` in its ping.
3. Add the package to `[tool.hatch.build.targets.wheel] packages` and a script to `[project.scripts]` in `pyproject.toml`.

### Releasing

Push to `main`: uvx installs the servers straight from the repo. When an app's in-app part changes, bump its version in both places named in that app's README so `<app>_status` tells users to update.
