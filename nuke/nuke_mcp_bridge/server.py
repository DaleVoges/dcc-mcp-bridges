"""
Nuke MCP server.

Exposes tools that let Claude drive a running Foundry Nuke session through the
`claude_mcp_bridge` module running inside Nuke (installed with `nuke-mcp-bridge install`).

Run (stdio transport):  uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges nuke-mcp-bridge
                        (or, from a checkout: python -m nuke_mcp_bridge)
Env vars: NUKE_MCP_HOST, NUKE_MCP_PORT (first port scanned, default 7577),
          NUKE_MCP_PORT_RANGE (default 10), NUKE_MCP_TIMEOUT (seconds, default 600).
"""
from __future__ import annotations

from dcc_mcp_core import Bridge, BridgeError, Image, SessionGoneError, create_server

from nuke_mcp_bridge import __version__

__all__ = ["BridgeError", "SessionGoneError", "bridge", "main", "mcp"]

INSTALL_COMMAND = "uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges nuke-mcp-bridge install"

INSTRUCTIONS = """\
You are connected to a live Foundry Nuke session.

Use `execute_python` for almost everything. The code runs inside Nuke on its main thread,
in a namespace that persists between calls, with `nuke` and `nukescripts` pre-imported.
The value of the last expression (or a variable named `result`) is returned as JSON
(nodes come back as their names, knobs as node.knob). print() output is returned too.
Each call is one undo step, so the user can Ctrl+Z it.

Several Nukes can be open at once. If only one is running it is used automatically.
If several are, tools refuse to run until one is chosen: call `list_nuke_sessions`, ask the
user which one (name it by its open script), then `select_nuke_session`. Never pick one
yourself. If a tool says the selected Nuke is gone, nothing was run; confirm with the user
before selecting another.

Tips:
- Call `nuke_status` first: Nuke 12 and older run Python 2.7 (no f-strings there), and
  Non-commercial/Indie licenses have limits.
- Inspect before you modify: `get_script_info`, `list_nodes`, `inspect_node`.
- Check a node class's exact knob names with `get_node_class_info` before setting knobs.
- Create nodes with nuke.nodes.<Class>(...) and wire them with setInput(); nuke.createNode()
  also auto-connects to the selection. Position new nodes (setXYpos, or
  nuke.autoplace) so the node graph stays readable.
- Use `render_frame` to see a node's output; call `find_node_errors` after building a tree.
- Do not open/save/close scripts, or render Write nodes to disk, unless the user asked.
"""


def _session_label(s: dict) -> str:
    return f"{s.get('product')} {s.get('nuke_version')}" + ("" if s.get("gui") else " (terminal mode)")


bridge = Bridge(
    "nuke", "Nuke", 7577, __version__, _session_label, scene_key="script",
    not_found_hint="Ask the user to open Nuke. If it is open, the bridge may not be installed: "
                   f"the user installs it by running `{INSTALL_COMMAND}` in a terminal and "
                   "restarting Nuke (or starts it from Edit > Claude MCP > Start Bridge).",
    update_hint=f"Tell the user to update it by running `{INSTALL_COMMAND}` and restarting Nuke.",
)
mcp = create_server("nuke", INSTRUCTIONS, __version__)

# Code sent into Nuke must also run on Python 2.7 (Nuke 12): no f-strings inside it.
_KNOB_HELPERS = r'''
def _knob_value(k):
    try:
        v = k.value()
    except Exception:
        try:
            v = k.getText()
        except Exception:
            return None
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return list(v)
    return str(v)
_SKIP_KNOBS = ("Tab_Knob", "Text_Knob", "PyScript_Knob", "Help_Knob", "Obsolete_Knob",
               "Link_Knob", "PythonCustomKnob", "Script_Knob")
'''


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@mcp.tool()
def nuke_status() -> str:
    """Reports the selected Nuke: version, product/license, Python version, port, pid, open script."""
    return bridge.status()


@mcp.tool()
def list_nuke_sessions() -> str:
    """Lists every running Nuke with the bridge started (port, version, pid, open script).

    When several are running, ask the user which one to use and call select_nuke_session.
    """
    return bridge.list_sessions()


@mcp.tool()
def select_nuke_session(port: int) -> str:
    """Chooses which Nuke the other tools talk to, by the port shown in list_nuke_sessions."""
    return bridge.select_session(port)


@mcp.tool()
def execute_python(code: str, undoable: bool = True, reset_namespace: bool = False) -> str:
    """Executes Python code inside Nuke, with `nuke` and `nukescripts` pre-imported.

    Variables persist between calls. The last expression's value (or a variable named
    `result`) is returned as JSON (nodes become their names), along with anything printed.

    Args:
        code: Python source to run. Multi-line is fine.
        undoable: Wrap the call in a single undo step (default True).
        reset_namespace: Clear variables left over from earlier calls first.
    """
    return bridge.run("python", code, undoable, reset_namespace)


@mcp.tool()
def execute_tcl(command: str, undoable: bool = True) -> str:
    """Runs a TCL command or expression inside Nuke (nuke.tcl) and returns its result."""
    return bridge.run("tcl", command, undoable)


@mcp.tool()
def get_script_info() -> str:
    """Summarises the open script: name, format, fps, frame range, colour management,
    selection, node counts by class, Read and Write nodes, and Viewer inputs."""
    return bridge.python(r'''
_root = nuke.root()
_all = nuke.allNodes(recurseGroups=True)
_counts = {}
for _n in _all:
    _counts[_n.Class()] = _counts.get(_n.Class(), 0) + 1
def _k(name):
    k = _root.knob(name)
    return k.value() if k is not None else None
result = {
    "script": _root["name"].value() or "Untitled",
    "modified": _root.modified(),
    "nuke": nuke.NUKE_VERSION_STRING,
    "format": {"name": _root.format().name(), "width": _root.format().width(),
               "height": _root.format().height(), "pixel_aspect": _root.format().pixelAspect()},
    "fps": _root.fps(),
    "frame_range": [_root.firstFrame(), _root.lastFrame()],
    "current_frame": nuke.frame(),
    "color_management": _k("colorManagement"),
    "ocio_config": _k("OCIO_config"),
    "working_space": _k("workingSpaceLUT"),
    "selected": [n.fullName() for n in nuke.selectedNodes()][:200],
    "node_count": len(_all),
    "node_classes": _counts,
    "reads": [{"node": n.fullName(), "file": n["file"].value(),
               "range": [n["first"].value(), n["last"].value()]}
              for n in _all if n.Class() == "Read"][:100],
    "writes": [{"node": n.fullName(), "file": n["file"].value(), "disabled": n["disable"].value()}
               for n in _all if n.Class() == "Write"][:100],
    "viewers": [{"node": n.fullName(),
                 "inputs": [n.input(i).fullName() if n.input(i) else None for i in range(n.inputs())]}
                for n in _all if n.Class() == "Viewer"],
}
''')


@mcp.tool()
def list_nodes(group: str = "root", node_class: str = "", pattern: str = "*",
               recursive: bool = False, selected_only: bool = False, limit: int = 500) -> str:
    """Lists nodes in the script or inside a Group/gizmo.

    Args:
        group: "root" for the top level, or a Group/gizmo node name, e.g. "Group1".
        node_class: Node class filter, e.g. "Read", "Grade", "Merge2", "Write".
        pattern: Node name wildcard, e.g. "Grade*" or "*_OUT".
        recursive: Include nodes inside Groups.
        selected_only: Only selected nodes.
        limit: Maximum number of nodes to return.
    """
    return bridge.python(rf'''
import fnmatch
_g = nuke.root() if {group!r} == "root" else nuke.toNode({group!r})
if _g is None:
    raise ValueError("No group named %r" % {group!r})
with _g:
    _nodes = nuke.selectedNodes() if {selected_only!r} else nuke.allNodes(recurseGroups={recursive!r})
_hits = [n for n in _nodes if fnmatch.fnmatchcase(n.name(), {pattern!r})
         and (not {node_class!r} or n.Class() == {node_class!r})]
result = {{"count": len(_hits),
          "nodes": [{{"name": n.fullName(), "class": n.Class(),
                     "inputs": [n.input(i).fullName() if n.input(i) else None for i in range(n.inputs())]}}
                    for n in _hits[:{int(limit)}]]}}
''')


@mcp.tool()
def inspect_node(name: str, all_knobs: bool = False) -> str:
    """Describes a node: class, inputs, dependents, knobs, errors, format, bbox, channels, frame range.

    Args:
        name: Node name, e.g. "Grade1" or "Group1.Blur1" for a node inside a group.
        all_knobs: List every knob. By default only knobs changed from their defaults,
            or driven by expressions/animation, are listed.
    """
    return bridge.python(_KNOB_HELPERS + rf'''
_n = nuke.toNode({name!r})
if _n is None:
    raise ValueError("No node named %r" % {name!r})
_info = {{
    "name": _n.fullName(),
    "class": _n.Class(),
    "inputs": [_n.input(i).fullName() if _n.input(i) else None for i in range(_n.inputs())],
    "dependents": [d.fullName() for d in _n.dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS, forceEvaluate=False)],
    "position": [_n.xpos(), _n.ypos()],
    "disabled": _n["disable"].value() if _n.knob("disable") else False,
}}
_knobs = {{}}
for _kn, _k in _n.knobs().items():
    if _k.Class() in _SKIP_KNOBS or _kn in ("xpos", "ypos", "selected", "name"):
        continue
    _anim = False
    _expr = None
    try:
        _anim = _k.isAnimated()
        _expr = _k.toScript() if _k.hasExpression() else None
    except Exception:
        pass
    try:
        _changed = _k.notDefault()
    except Exception:
        _changed = True
    if not {all_knobs!r} and not (_changed or _anim or _expr):
        continue
    _e = {{"value": _knob_value(_k), "type": _k.Class()}}
    if _expr:
        _e["expression"] = _expr
    elif _anim:
        _e["animated"] = True
        try:
            _e["script"] = _k.toScript()[:500]
        except Exception:
            pass
    _knobs[_kn] = _e
_info["knobs"] = _knobs
try:
    _info["has_error"] = bool(_n.hasError())
except Exception:
    pass
try:
    _f = _n.format()
    _info["format"] = {{"name": _f.name(), "width": _f.width(), "height": _f.height()}}
except Exception:
    pass
try:
    _b = _n.bbox()
    _info["bbox"] = {{"x": _b.x(), "y": _b.y(), "w": _b.w(), "h": _b.h()}}
except Exception:
    pass
try:
    _info["channels"] = list(_n.channels())[:200]
    _info["frame_range"] = [_n.firstFrame(), _n.lastFrame()]
except Exception:
    pass
try:
    _md = _n.metadata()
    _info["metadata_keys"] = sorted(_md.keys())[:100] if _md else []
except Exception:
    pass
result = _info
''')


@mcp.tool()
def get_node_class_info(node_class: str) -> str:
    """Lists a node class's knobs (names, types, labels, defaults, menu items) and inputs.

    Creates a temporary node to read them (then deletes it; not recorded in undo).
    Use it before setting knobs, since knob names often differ from their labels.

    Args:
        node_class: e.g. "Grade", "Merge2", "Transform", "Blur", "Write", "ColorCorrect".
    """
    return bridge.python(_KNOB_HELPERS + rf'''
nuke.Undo.disable()
try:
    with nuke.root():
        _n = nuke.createNode({node_class!r}, inpanel=False)
    try:
        _knobs = []
        for _k in _n.allKnobs():
            if _k.Class() in _SKIP_KNOBS or _k.name() in ("xpos", "ypos", "selected"):
                continue
            _e = {{"name": _k.name(), "label": _k.label() or _k.name(), "type": _k.Class(),
                  "default": _knob_value(_k)}}
            if hasattr(_k, "values"):
                try:
                    _e["menu"] = list(_k.values())[:60]
                except Exception:
                    pass
            _tip = _k.tooltip() if hasattr(_k, "tooltip") else ""
            if _tip:
                _e["tooltip"] = _tip[:300]
            _knobs.append(_e)
        result = {{"class": _n.Class(), "max_inputs": _n.maxInputs(), "min_inputs": _n.minInputs(),
                  "knobs": _knobs}}
    finally:
        nuke.delete(_n)
finally:
    nuke.Undo.enable()
''')


@mcp.tool()
def get_nuke_help(name: str) -> str:
    """Returns the Python docs for a nuke module member, e.g. "createNode", "Node", "Node.setInput",
    "Knob" or "nodes"."""
    return bridge.python(rf'''
import pydoc
_obj = nuke
for _part in {name!r}.replace("nuke.", "", 1).split("."):
    _obj = getattr(_obj, _part)
result = pydoc.plain(pydoc.render_doc(_obj))[:20000]
''')


@mcp.tool()
def find_node_errors(recursive: bool = True, limit: int = 200) -> str:
    """Lists nodes that are in an error state (missing files, bad expressions, invalid inputs...)."""
    return bridge.python(rf'''
_found = []
for _n in nuke.allNodes(recurseGroups={recursive!r}):
    try:
        if _n.hasError():
            _e = {{"node": _n.fullName(), "class": _n.Class()}}
            if _n.Class() == "Read":
                _e["file"] = _n["file"].evaluate()
            _found.append(_e)
    except Exception:
        pass
result = {{"count": len(_found), "nodes": _found[:{int(limit)}]}}
''')


@mcp.tool()
def render_frame(node: str = "", frame: int | None = None, width: int = 960) -> Image:
    """Renders one frame of a node's output and returns it as an image, so you can see it.

    Uses temporary Reformat and Write nodes (deleted afterwards, not recorded in undo);
    nothing in the script changes. Works without a GUI too.

    Args:
        node: Node to render. Default: whatever the active Viewer shows, else the selected node.
        frame: Frame number. Default: the current frame.
        width: Output width in pixels (height follows the aspect ratio).
    """
    return bridge.image({"cmd": "capture", "node": node or None, "frame": frame,
                         "width": width})


def main() -> None:
    import sys
    args = sys.argv[1:]
    if args and args[0] in ("install", "uninstall"):
        import argparse
        from nuke_mcp_bridge import install as installer
        parser = argparse.ArgumentParser(prog=f"nuke-mcp-bridge {args[0]}")
        parser.add_argument("--nuke-dir", help="The .nuke folder to use. Default: ~/.nuke")
        opts = parser.parse_args(args[1:])
        (installer.install if args[0] == "install" else installer.uninstall)(opts.nuke_dir)
        return
    if args and args[0] in ("--version", "-V"):
        print(__version__)
        return
    mcp.run()


if __name__ == "__main__":
    main()
