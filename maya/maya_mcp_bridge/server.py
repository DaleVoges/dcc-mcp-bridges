"""
Maya MCP server.

Exposes tools that let Claude drive a running Autodesk Maya session through the
`claude_mcp_bridge` Maya plug-in (which must be loaded in Maya).

Run (stdio transport):  uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges maya-mcp-bridge
                        (or, from a checkout: python -m maya_mcp_bridge)
Env vars: MAYA_MCP_HOST, MAYA_MCP_PORT (first port scanned, default 7377),
          MAYA_MCP_PORT_RANGE (default 10), MAYA_MCP_TIMEOUT (seconds, default 600).
"""
from __future__ import annotations

from dcc_mcp_core import Bridge, BridgeError, Image, SessionGoneError, create_server

from maya_mcp_bridge import __version__

__all__ = ["BridgeError", "SessionGoneError", "bridge", "main", "mcp"]

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


def _session_label(s: dict) -> str:
    return f"Maya {s.get('maya_version')}" + (" batch" if s.get("batch") else "")


bridge = Bridge(
    "maya", "Maya", 7377, __version__, _session_label,
    not_found_hint="Ask the user to open Maya and load the 'claude_mcp_bridge' plug-in "
                   "(Windows > Settings/Preferences > Plug-in Manager).",
    update_hint="Tell the user to update the plug-in by dragging the latest drag_to_install.py "
                "into Maya.",
    accept_missing_app=True,  # the Maya plug-in's ping has no "app" field
)
mcp = create_server("maya", INSTRUCTIONS, __version__)


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@mcp.tool()
def maya_status() -> str:
    """Reports the selected Maya: version, Python version, port, pid and open scene."""
    return bridge.status()


@mcp.tool()
def list_maya_sessions() -> str:
    """Lists every running Maya with the bridge loaded (port, version, pid, open scene).

    When several are running, ask the user which one to use and call select_maya_session.
    """
    return bridge.list_sessions()


@mcp.tool()
def select_maya_session(port: int) -> str:
    """Chooses which Maya the other tools talk to, by the port shown in list_maya_sessions."""
    return bridge.select_session(port)


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
    return bridge.run("python", code, undoable, reset_namespace)


@mcp.tool()
def execute_mel(code: str, undoable: bool = True) -> str:
    """Executes MEL code inside Maya and returns its result."""
    return bridge.run("mel", code, undoable)


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
    return bridge.python(code)


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
    return bridge.python(code)


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
    return bridge.python(code)


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
    return bridge.python(code)


@mcp.tool()
def capture_viewport(width: int = 960, height: int = 540, camera: str = "") -> Image:
    """Takes a screenshot (playblast of the current frame) of the active Maya viewport.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        camera: Optional camera to look through for the capture (restored afterwards).
    """
    return bridge.image({"cmd": "capture", "width": width, "height": height,
                         "camera": camera or None})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
