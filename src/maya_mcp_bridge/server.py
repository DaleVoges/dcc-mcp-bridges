"""
Maya MCP server.

Exposes tools that let Claude drive a running Autodesk Maya session through the
`claude_mcp_bridge` Maya plug-in (which must be loaded in Maya).

Run (stdio transport):  uvx maya-mcp-bridge    (or: python -m maya_mcp_bridge)
Env vars:
    MAYA_MCP_HOST     (default 127.0.0.1)
    MAYA_MCP_PORT        first port to scan for Mayas (default 7377)
    MAYA_MCP_PORT_RANGE  how many ports to scan (default 10)
    MAYA_MCP_TIMEOUT  seconds to wait for a Maya call (default 600)
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError

from maya_mcp_bridge import __version__

HOST = os.environ.get("MAYA_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAYA_MCP_PORT", "7377"))            # first port scanned
PORT_RANGE = int(os.environ.get("MAYA_MCP_PORT_RANGE", "10"))  # number of ports scanned
TIMEOUT = float(os.environ.get("MAYA_MCP_TIMEOUT", "600"))

INSTRUCTIONS = """\
You are connected to a live Autodesk Maya session.

Use `execute_python` for almost everything. The code runs inside Maya on its main
thread, in a namespace that persists between calls, with these names pre-imported:
  cmds / mc  -> maya.cmds
  mel        -> maya.mel
  om / om2   -> maya.api.OpenMaya (API 2.0)     om1 -> maya.OpenMaya (API 1.0)
  oma        -> maya.api.OpenMayaAnim
  pm         -> pymel.core (lazy-imported; PyMEL is an optional install in Maya 2022+)
The value of the last expression (or a variable named `result`) is returned as JSON.
print() output is returned too. Each call is one undo chunk, so the user can Ctrl+Z it.

Several Mayas can be open at once. If only one is running it is used automatically.
If several are, tools refuse to run until one is chosen: call `list_maya_sessions`, ask the
user which one (name it by its open scene), then `select_maya_session`. Never pick one yourself.
If a tool says the selected Maya is gone, nothing was run; confirm with the user before
selecting another.

Tips:
- Call `maya_status` first to learn the Maya and Python versions (Maya <= 2020 is Python 2.7:
  no f-strings there).
- Prefer maya.cmds for scene edits; use OpenMaya for heavy per-vertex/per-component work.
- Inspect before you modify: `get_scene_info`, `list_nodes`, `inspect_node`.
- Use `capture_viewport` to look at the result.
- Do not open/save/close scenes or delete large amounts of data unless the user asked.
"""

mcp = FastMCP("maya", instructions=INSTRUCTIONS, version=__version__) \
    if "version" in FastMCP.__init__.__code__.co_varnames else FastMCP("maya", instructions=INSTRUCTIONS)


# ----------------------------------------------------------------------------
# Bridge client
# ----------------------------------------------------------------------------
class MayaBridgeError(ToolError):
    """Raised for Maya-side errors; ToolError makes the message reach Claude verbatim."""


class SessionGoneError(MayaBridgeError):
    """The selected Maya is no longer reachable (closed, or its port now belongs to another Maya)."""


_ids = itertools.count(1)


def _exchange(fh, payload: dict[str, Any]) -> dict[str, Any]:
    fh.write((json.dumps(dict(payload, id=next(_ids))) + "\n").encode("utf-8"))
    fh.flush()
    line = fh.readline()
    if not line:
        raise ConnectionError("Maya closed the connection")
    return json.loads(line.decode("utf-8"))


def probe(port: int) -> dict[str, Any] | None:
    """Returns the session info of the Maya bridge on `port`, or None if nothing answers."""
    try:
        with socket.create_connection((HOST, port), timeout=0.5) as sock:
            sock.settimeout(3)
            with sock.makefile("rwb") as fh:
                resp = _exchange(fh, {"cmd": "ping"})
        return resp.get("result") if resp.get("ok") else None
    except (OSError, ValueError):
        return None


def scan() -> list[dict[str, Any]]:
    """Finds every Maya bridge in the port range (probed in parallel)."""
    ports = range(PORT, PORT + PORT_RANGE)
    with ThreadPoolExecutor(max_workers=len(ports) or 1) as pool:
        return [info for info in pool.map(probe, ports) if info]


def _describe(sessions: list[dict[str, Any]], selected_pid: int | None = None) -> str:
    lines = []
    for s in sessions:
        mark = "  <- selected" if s.get("pid") == selected_pid else ""
        busy = "  (busy)" if s.get("busy") else ""
        mode = " batch" if s.get("batch") else ""
        lines.append(f"- port {s['port']}: Maya {s.get('maya_version')}{mode}, pid {s['pid']}, "
                     f"scene: {s.get('scene')}{busy}{mark}")
    return "\n".join(lines)


class MayaClient:
    """Keeps one TCP connection to one Maya bridge (identified by port + pid)."""

    def __init__(self, host: str, port: int, timeout: float, pid: int | None = None):
        self.host, self.port, self.timeout, self.pid = host, port, timeout, pid
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as exc:
            raise SessionGoneError(
                f"Cannot reach Maya at {self.host}:{self.port} ({exc}). It may have been closed. "
                "Call list_maya_sessions to see which Mayas are available."
            ) from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._file = sock.makefile("rwb")
        if self.pid is not None:
            # Make sure this port still belongs to the Maya that was selected.
            try:
                info = _exchange(self._file, {"cmd": "ping"}).get("result") or {}
            except (OSError, ValueError) as exc:
                self._close()
                raise SessionGoneError(f"Maya on port {self.port} stopped answering ({exc}).") from exc
            if info.get("pid") != self.pid:
                self._close()
                raise SessionGoneError(
                    f"The selected Maya (pid {self.pid}) is gone; port {self.port} now belongs to "
                    f"Maya pid {info.get('pid')} with scene {info.get('scene')}. Nothing was run. "
                    "Call list_maya_sessions and confirm with the user which Maya to use."
                )

    def _close(self) -> None:
        for obj in (self._file, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._sock = self._file = None

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for attempt in range(2):
                if self._sock is None:
                    self._connect()
                try:
                    return _exchange(self._file, payload)
                except socket.timeout as exc:
                    self._close()
                    raise MayaBridgeError(
                        f"Maya did not answer within {self.timeout:.0f}s "
                        "(it may still be busy running the command)."
                    ) from exc
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 1:
                        raise SessionGoneError("Lost connection to Maya.")
            raise MayaBridgeError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._close()


class Sessions:
    """Routes calls to the selected Maya; auto-selects only when exactly one is running."""

    def __init__(self) -> None:
        self.current: MayaClient | None = None
        self._lock = threading.Lock()

    def select(self, info: dict[str, Any]) -> None:
        with self._lock:
            if self.current is not None:
                self.current.close()
            self.current = MayaClient(HOST, info["port"], TIMEOUT, info["pid"])

    def _client(self) -> MayaClient:
        if self.current is not None:
            return self.current
        found = scan()
        if not found:
            raise MayaBridgeError(
                f"No Maya found on ports {PORT}-{PORT + PORT_RANGE - 1}. Ask the user to open Maya and "
                "load the 'claude_mcp_bridge' plug-in (Windows > Settings/Preferences > Plug-in Manager)."
            )
        if len(found) > 1:
            raise MayaBridgeError(
                f"{len(found)} Maya sessions are running and none is selected yet, so nothing was run:\n"
                f"{_describe(found)}\n"
                "Ask the user which one to use, then call select_maya_session(port=...)."
            )
        self.select(found[0])
        return self.current

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._client()
        try:
            return client.call(payload)
        except SessionGoneError:
            with self._lock:
                if self.current is client:
                    self.current = None
            raise


maya = Sessions()


def _format(resp: dict[str, Any]) -> str:
    """Turns a bridge response into text for Claude; raises on Maya-side errors."""
    parts = []
    if resp.get("stdout"):
        parts.append("stdout:\n" + resp["stdout"].rstrip())
    if not resp.get("ok"):
        parts.append("error:\n" + (resp.get("error") or "unknown error").rstrip())
        raise MayaBridgeError("\n\n".join(parts))
    result = resp.get("result")
    if result is not None:
        parts.append("result:\n" + json.dumps(result, indent=2, ensure_ascii=False))
    return "\n\n".join(parts) or "OK (no output)"


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@mcp.tool()
def maya_status() -> str:
    """Reports the selected Maya: version, Python version, port, pid and open scene."""
    resp = maya.call({"cmd": "ping"})
    text = _format(resp)
    plugin_version = (resp.get("result") or {}).get("bridge_version")
    if plugin_version != __version__:
        text += (f"\n\nNote: the Maya plug-in is version {plugin_version} but this server is "
                 f"{__version__}. Tell the user to update the plug-in by dragging the latest "
                 "drag_to_install.py into Maya.")
    return text


@mcp.tool()
def list_maya_sessions() -> str:
    """Lists every running Maya with the bridge loaded (port, version, pid, open scene).

    When several are running, ask the user which one to use and call select_maya_session.
    """
    found = scan()
    if not found:
        return f"No Maya found on ports {PORT}-{PORT + PORT_RANGE - 1}."
    selected = maya.current.pid if maya.current else None
    note = "" if selected else "\n\nNo session selected yet."
    return _describe(found, selected) + note


@mcp.tool()
def select_maya_session(port: int) -> str:
    """Chooses which Maya the other tools talk to, by the port shown in list_maya_sessions."""
    info = probe(port)
    if info is None:
        raise MayaBridgeError(f"No Maya bridge answering on port {port}. Call list_maya_sessions.")
    maya.select(info)
    return "Selected:\n" + _describe([info], info["pid"])


@mcp.tool()
def execute_python(code: str, undoable: bool = True, reset_namespace: bool = False) -> str:
    """Executes Python code inside Maya (maya.cmds, PyMEL, OpenMaya).

    Pre-imported: cmds/mc, mel, om/om2 (maya.api.OpenMaya), om1 (maya.OpenMaya),
    oma (maya.api.OpenMayaAnim), pm (pymel.core, lazy). Variables persist between calls.
    The last expression's value (or a variable named `result`) is returned as JSON,
    along with anything printed.

    Args:
        code: Python source to run. Multi-line is fine.
        undoable: Wrap the call in a single undo chunk (default True).
        reset_namespace: Clear variables left over from earlier calls first.
    """
    return _format(maya.call({"cmd": "python", "code": code, "undo": undoable,
                              "reset": reset_namespace}))


@mcp.tool()
def execute_mel(code: str, undoable: bool = True) -> str:
    """Executes MEL code inside Maya and returns its result."""
    return _format(maya.call({"cmd": "mel", "code": code, "undo": undoable}))


@mcp.tool()
def get_scene_info() -> str:
    """Summarises the open scene: file, units, frame range, selection, cameras, node counts."""
    code = r'''
_sel = cmds.ls(selection=True, long=True) or []
_counts = {}
for _t in ("transform", "mesh", "nurbsCurve", "nurbsSurface", "joint", "camera", "light",
           "shadingEngine", "animCurve", "constraint", "reference"):
    _counts[_t] = len(cmds.ls(type=_t) or [])
result = {
    "scene": cmds.file(query=True, sceneName=True) or "untitled",
    "modified": cmds.file(query=True, modified=True),
    "linear_unit": cmds.currentUnit(query=True, linear=True),
    "angular_unit": cmds.currentUnit(query=True, angle=True),
    "time_unit": cmds.currentUnit(query=True, time=True),
    "up_axis": cmds.upAxis(query=True, axis=True),
    "frame_range": [cmds.playbackOptions(query=True, minTime=True),
                    cmds.playbackOptions(query=True, maxTime=True)],
    "current_frame": cmds.currentTime(query=True),
    "selection": _sel[:200],
    "top_level_dag": (cmds.ls(assemblies=True, long=True) or [])[:200],
    "cameras": cmds.ls(cameras=True) or [],
    "node_counts": _counts,
    "loaded_references": [r for r in (cmds.ls(type="reference") or [])
                          if r != "sharedReferenceNode"],
}
'''
    return _format(maya.call({"cmd": "python", "code": code, "undo": False}))


@mcp.tool()
def list_nodes(node_type: str = "", pattern: str = "*", selected_only: bool = False,
               dag_only: bool = False, long_names: bool = False, limit: int = 500) -> str:
    """Lists nodes in the scene (wraps cmds.ls).

    Args:
        node_type: Maya node type filter, e.g. "mesh", "joint", "transform", "camera".
        pattern: Name wildcard, e.g. "*_ctrl" or "char:*".
        selected_only: Only consider the current selection.
        dag_only: Only DAG nodes.
        long_names: Return full DAG paths.
        limit: Maximum number of names to return.
    """
    kwargs = {"long": long_names, "selection": selected_only, "dag": dag_only}
    if node_type:
        kwargs["type"] = node_type
    code = (f"_n = cmds.ls({pattern!r}, **{kwargs!r}) or []\n"
            f"result = {{'count': len(_n), 'nodes': _n[:{int(limit)}]}}")
    return _format(maya.call({"cmd": "python", "code": code, "undo": False}))


@mcp.tool()
def inspect_node(node: str, include_values: bool = True, user_defined_only: bool = False) -> str:
    """Describes a node: type, hierarchy, shapes, connections and keyable/user attribute values.

    Args:
        node: Node name or DAG path.
        include_values: Read the values of the listed attributes.
        user_defined_only: Only list custom (user-defined) attributes.
    """
    code = rf'''
_node = {node!r}
if not cmds.objExists(_node):
    raise ValueError("Node does not exist: %s" % _node)
_info = {{
    "name": _node,
    "type": cmds.nodeType(_node),
    "inherited_types": cmds.nodeType(_node, inherited=True),
}}
if cmds.objectType(_node, isAType="dagNode"):
    _info["long_name"] = (cmds.ls(_node, long=True) or [_node])[0]
    _info["parent"] = cmds.listRelatives(_node, parent=True, fullPath=True)
    _info["children"] = cmds.listRelatives(_node, children=True, fullPath=True)
    _info["shapes"] = cmds.listRelatives(_node, shapes=True, fullPath=True)
    if cmds.objectType(_node, isAType="transform"):
        _info["world_matrix"] = cmds.xform(_node, query=True, matrix=True, worldSpace=True)
        _info["bbox_world"] = cmds.exactWorldBoundingBox(_node)
_attrs = cmds.listAttr(_node, userDefined=True) if {user_defined_only!r} else \
    sorted(set((cmds.listAttr(_node, keyable=True) or []) +
               (cmds.listAttr(_node, userDefined=True) or [])))
_attrs = _attrs or []
if {include_values!r}:
    _vals = {{}}
    for _a in _attrs:
        try:
            _vals[_a] = cmds.getAttr(_node + "." + _a)
        except Exception as _e:
            _vals[_a] = "<unreadable: %s>" % _e
    _info["attributes"] = _vals
else:
    _info["attributes"] = _attrs
_info["incoming"] = cmds.listConnections(_node, source=True, destination=False,
                                         plugs=True, connections=True) or []
_info["outgoing"] = cmds.listConnections(_node, source=False, destination=True,
                                         plugs=True, connections=True) or []
result = _info
'''
    return _format(maya.call({"cmd": "python", "code": code, "undo": False}))


@mcp.tool()
def get_command_help(command: str, api: str = "cmds") -> str:
    """Looks up documentation inside Maya for a command or class.

    Args:
        command: e.g. "polyCube", "MFnMesh", "PyNode".
        api: "cmds" (flags from cmds.help), "mel" (whatIs), "om" (maya.api.OpenMaya docstring),
             or "pymel" (pymel.core docstring).
    """
    code = rf'''
_name, _api = {command!r}, {api!r}
if _api == "cmds":
    result = cmds.help(_name)
elif _api == "mel":
    result = mel.eval('whatIs "%s"' % _name)
elif _api == "om":
    import pydoc
    result = pydoc.render_doc(getattr(om, _name), renderer=pydoc.plaintext)[:20000]
elif _api == "pymel":
    import pydoc
    result = pydoc.render_doc(getattr(pm, _name), renderer=pydoc.plaintext)[:20000]
else:
    raise ValueError("api must be cmds, mel, om or pymel")
'''
    return _format(maya.call({"cmd": "python", "code": code, "undo": False}))


@mcp.tool()
def capture_viewport(width: int = 960, height: int = 540, camera: str = "") -> Image:
    """Takes a screenshot (playblast of the current frame) of the active Maya viewport.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        camera: Optional camera to look through for the capture (restored afterwards).
    """
    resp = maya.call({"cmd": "capture", "width": width, "height": height,
                      "camera": camera or None})
    if not resp.get("ok"):
        _format(resp)
    return Image(data=base64.b64decode(resp["result"]["base64"]), format="png")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
