"""
Houdini MCP server.

Exposes tools that let Claude drive a running SideFX Houdini session through the
`claude_mcp_bridge` module running inside Houdini (installed with `houdini-mcp-bridge install`).

Run (stdio transport):  uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges houdini-mcp-bridge
                        (or, from a checkout: python -m houdini_mcp_bridge)
Env vars: HOUDINI_MCP_HOST, HOUDINI_MCP_PORT (first port scanned, default 7477),
          HOUDINI_MCP_PORT_RANGE (default 10), HOUDINI_MCP_TIMEOUT (seconds, default 600).
"""
from __future__ import annotations

from dcc_mcp_core import Bridge, BridgeError, Image, SessionGoneError, create_server

from houdini_mcp_bridge import __version__

__all__ = ["BridgeError", "SessionGoneError", "bridge", "main", "mcp"]

INSTALL_COMMAND = "uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges houdini-mcp-bridge install"

INSTRUCTIONS = """\
You are connected to a live SideFX Houdini session.

Use `execute_python` for almost everything. The code runs inside Houdini on its main
thread, in a namespace that persists between calls, with `hou` pre-imported.
The value of the last expression (or a variable named `result`) is returned as JSON
(nodes and parms come back as their paths). print() output is returned too.
Each call is one undo group, so the user can Ctrl+Z it.

Several Houdinis can be open at once. If only one is running it is used automatically.
If several are, tools refuse to run until one is chosen: call `list_houdini_sessions`, ask the
user which one (name it by its open .hip file), then `select_houdini_session`. Never pick one
yourself. If a tool says the selected Houdini is gone, nothing was run; confirm with the user
before selecting another.

Tips:
- Inspect before you modify: `get_scene_info`, `list_nodes`, `inspect_node`.
- Before creating nodes, check the exact type name and parm names with `get_node_type_info`
  (e.g. "box" in category "Sop"); parm names often differ from their UI labels.
- Lay out new nodes with node.moveToGoodPosition() or parent.layoutChildren(), and set the
  display/render flags on the node that should show.
- After building a network, call `find_node_errors` to check it cooks.
- Use `capture_viewport` to look at the result (needs graphical Houdini).
- Do not load/save/clear .hip files or delete large parts of the scene unless the user asked.
- Check `houdini_status` for the license: Apprentice and Indie have limits on file formats.
"""


def _session_label(s: dict) -> str:
    return f"{s.get('product')} {s.get('houdini_version')}" + ("" if s.get("ui") else " (no UI)")


bridge = Bridge(
    "houdini", "Houdini", 7477, __version__, _session_label,
    not_found_hint="Ask the user to open Houdini. If it is open, the bridge may not be "
                   f"installed: the user installs it by running `{INSTALL_COMMAND}` in a terminal "
                   "and restarting Houdini.",
    update_hint=f"Tell the user to update it by running `{INSTALL_COMMAND}` and restarting Houdini.",
)
mcp = create_server("houdini", INSTRUCTIONS, __version__)


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@mcp.tool()
def houdini_status() -> str:
    """Reports the selected Houdini: version, product, license, Python version, port, pid, .hip file."""
    return bridge.status()


@mcp.tool()
def list_houdini_sessions() -> str:
    """Lists every running Houdini with the bridge started (port, version, pid, open .hip file).

    When several are running, ask the user which one to use and call select_houdini_session.
    """
    return bridge.list_sessions()


@mcp.tool()
def select_houdini_session(port: int) -> str:
    """Chooses which Houdini the other tools talk to, by the port shown in list_houdini_sessions."""
    return bridge.select_session(port)


@mcp.tool()
def execute_python(code: str, undoable: bool = True, reset_namespace: bool = False) -> str:
    """Executes Python code inside Houdini, with `hou` pre-imported.

    Variables persist between calls. The last expression's value (or a variable named
    `result`) is returned as JSON (hou nodes/parms become their paths), along with anything
    printed.

    Args:
        code: Python source to run. Multi-line is fine.
        undoable: Wrap the call in a single undo group (default True).
        reset_namespace: Clear variables left over from earlier calls first.
    """
    return bridge.run("python", code, undoable, reset_namespace)


@mcp.tool()
def execute_hscript(command: str, undoable: bool = True) -> str:
    """Runs an HScript command inside Houdini (hou.hscript) and returns its output."""
    return bridge.run("hscript", command, undoable)


@mcp.tool()
def get_scene_info() -> str:
    """Summarises the open scene: .hip file, frame range, fps, selection, node counts per context."""
    return bridge.python(r'''
def _frange():
    try:
        return list(hou.playbar.frameRange())
    except Exception:
        return [hou.hscriptExpression("$FSTART"), hou.hscriptExpression("$FEND")]
_contexts = {}
for _c in hou.node("/").children():
    _contexts[_c.path()] = {"children": len(_c.children()),
                            "all_nodes": len(_c.allSubChildren(recurse_in_locked_nodes=False))}
result = {
    "hip_file": hou.hipFile.path(),
    "unsaved_changes": hou.hipFile.hasUnsavedChanges(),
    "houdini": hou.applicationVersionString(),
    "frame_range": _frange(),
    "current_frame": hou.frame(),
    "fps": hou.fps(),
    "selected_nodes": [n.path() for n in hou.selectedNodes()][:200],
    "contexts": _contexts,
    "obj_children": [n.path() + " (" + n.type().name() + ")" for n in hou.node("/obj").children()][:200],
}
''')


@mcp.tool()
def list_nodes(root: str = "/obj", pattern: str = "*", node_type: str = "",
               recursive: bool = False, limit: int = 500) -> str:
    """Lists nodes under a network path.

    Args:
        root: Network to list, e.g. "/obj", "/obj/geo1", "/stage", "/out", "/mat".
        pattern: Node name wildcard, e.g. "box*" or "*_OUT".
        node_type: Node type name wildcard, e.g. "box", "null", "*::labs*" (see inspect_node).
        recursive: Include nodes inside subnetworks (not inside locked HDAs).
        limit: Maximum number of nodes to return.
    """
    return bridge.python(rf'''
import fnmatch
_root = hou.node({root!r})
if _root is None:
    raise ValueError("No node at %s" % {root!r})
_nodes = _root.allSubChildren(recurse_in_locked_nodes=False) if {recursive!r} else _root.children()
_hits = [n for n in _nodes if fnmatch.fnmatchcase(n.name(), {pattern!r})
         and (not {node_type!r} or fnmatch.fnmatchcase(n.type().name(), {node_type!r}))]
result = {{"count": len(_hits),
          "nodes": [{{"path": n.path(), "type": n.type().name(),
                     "category": n.type().category().name()}} for n in _hits[:{int(limit)}]]}}
''')


@mcp.tool()
def inspect_node(path: str, all_parms: bool = False, geometry: bool = True) -> str:
    """Describes a node: type, flags, inputs/outputs, parms, errors, and (for SOPs) its geometry.

    Args:
        path: Node path, e.g. "/obj/geo1/box1".
        all_parms: List every parm. By default only parms changed from their defaults,
            or driven by expressions/keyframes, are listed.
        geometry: For SOP nodes, cook and summarise the output geometry (counts, attributes, bbox).
    """
    return bridge.python(rf'''
_n = hou.node({path!r})
if _n is None:
    raise ValueError("No node at %s" % {path!r})
_t = _n.type()
_info = {{
    "path": _n.path(),
    "type": _t.name(),
    "category": _t.category().name(),
    "description": _t.description(),
    "is_hda": _t.definition() is not None,
    "comment": _n.comment(),
    "inputs": [None if i is None else i.path() for i in _n.inputs()],
    "outputs": [o.path() for o in _n.outputs()],
    "children": [c.name() for c in _n.children()][:200],
}}
_flags = {{}}
for _f in ("isDisplayFlagSet", "isRenderFlagSet", "isBypassed", "isTemplateFlagSet"):
    if hasattr(_n, _f):
        try:
            _flags[_f[2:].replace("FlagSet", "").replace("Set", "").lower()] = getattr(_n, _f)()
        except Exception:
            pass
_info["flags"] = _flags
_parms = {{}}
for _p in _n.parms():
    _tpl = _p.parmTemplate()
    if _tpl.type() in (hou.parmTemplateType.Folder, hou.parmTemplateType.FolderSet,
                       hou.parmTemplateType.Separator, hou.parmTemplateType.Label):
        continue
    _expr = None
    try:
        _expr = _p.expression()
    except Exception:
        pass
    _keyed = len(_p.keyframes()) > 1
    if not {all_parms!r} and _p.isAtDefault() and _expr is None:
        continue
    try:
        _val = _p.eval()
    except Exception as _e:
        _val = "<error: %s>" % _e
    _entry = {{"label": _p.description(), "value": _val}}
    if _expr is not None:
        _entry["expression"] = _expr
    if _keyed:
        _entry["animated"] = True
    try:
        _ref = _p.getReferencedParm()
        if _ref is not None and _ref.path() != _p.path():
            _entry["references"] = _ref.path()
    except Exception:
        pass
    _parms[_p.name()] = _entry
_info["parms"] = _parms
try:
    _info["errors"] = list(_n.errors())
    _info["warnings"] = list(_n.warnings())
except Exception:
    pass
if isinstance(_n, hou.ObjNode):
    _info["world_transform"] = _n.worldTransform().asTuple()
if {geometry!r} and isinstance(_n, hou.SopNode):
    try:
        _g = _n.geometry()
        if _g is not None:
            _bb = _g.boundingBox()
            _info["geometry"] = {{
                "points": _g.intrinsicValue("pointcount"),
                "primitives": _g.intrinsicValue("primitivecount"),
                "vertices": _g.intrinsicValue("vertexcount"),
                "point_attribs": [a.name() for a in _g.pointAttribs()],
                "prim_attribs": [a.name() for a in _g.primAttribs()],
                "vertex_attribs": [a.name() for a in _g.vertexAttribs()],
                "detail_attribs": [a.name() for a in _g.globalAttribs()],
                "point_groups": [g.name() for g in _g.pointGroups()],
                "prim_groups": [g.name() for g in _g.primGroups()],
                "bbox": {{"min": list(_bb.minvec()), "max": list(_bb.maxvec())}},
            }}
    except Exception as _e:
        _info["geometry"] = "<could not cook: %s>" % _e
if isinstance(_n, hou.LopNode):
    try:
        _stage = _n.stage()
        _info["stage_root_prims"] = [str(p.GetPath()) for p in _stage.GetPseudoRoot().GetChildren()][:200]
    except Exception as _e:
        _info["stage"] = "<could not cook: %s>" % _e
result = _info
''')


@mcp.tool()
def get_node_type_info(node_type: str, category: str = "Sop", include_parms: bool = True) -> str:
    """Looks up a node type: its exact name, description, inputs and parm names, types and defaults.

    Use it before creating nodes, since parm names often differ from their UI labels.

    Args:
        node_type: Type name or a wildcard to search, e.g. "box", "attribwrangle", "*scatter*".
        category: "Sop", "Object", "Lop", "Dop", "Cop", "Cop2", "Top", "Driver" (ROPs),
            "Vop", "Chop" or "Shop".
        include_parms: List the parm templates (name, label, type, default).
    """
    return bridge.python(rf'''
import fnmatch
_cats = hou.nodeTypeCategories()
_cat = _cats.get({category!r})
if _cat is None:
    raise ValueError("Unknown category %r. Known: %s" % ({category!r}, sorted(_cats)))
_types = _cat.nodeTypes()
_name = {node_type!r}
if _name in _types:
    _t = _types[_name]
else:
    _hits = sorted(n for n, t in _types.items()
                   if fnmatch.fnmatchcase(n, _name) or fnmatch.fnmatchcase(t.description().lower(), _name.lower()))
    if len(_hits) != 1:
        result = {{"matches": _hits[:200], "note": "Pass one exact type name for details."}}
        _t = None
    else:
        _t = _types[_hits[0]]
if _t is not None:
    _info = {{"name": _t.name(), "description": _t.description(), "category": _cat.name(),
             "min_inputs": _t.minNumInputs(), "max_inputs": _t.maxNumInputs(),
             "is_hda": _t.definition() is not None, "deprecated": _t.deprecated()}}
    if {include_parms!r}:
        _out = []
        def _walk(templates, folder=""):
            for _tp in templates:
                _ty = _tp.type()
                if _ty == hou.parmTemplateType.Folder:
                    _walk(_tp.parmTemplates(), folder + _tp.label() + "/")
                    continue
                if _ty in (hou.parmTemplateType.FolderSet, hou.parmTemplateType.Separator,
                           hou.parmTemplateType.Label):
                    continue
                _e = {{"name": _tp.name(), "label": _tp.label(), "type": _ty.name(),
                      "size": _tp.numComponents(), "folder": folder.rstrip("/")}}
                for _attr in ("defaultValue", "defaultExpression"):
                    try:
                        _v = getattr(_tp, _attr)()
                        if _v not in ((), "", None) and any(_v if isinstance(_v, tuple) else [_v]):
                            _e["default" if _attr == "defaultValue" else "default_expression"] = _v
                    except Exception:
                        pass
                try:
                    _items = _tp.menuItems()
                    if _items:
                        _e["menu"] = list(_items)[:50]
                except Exception:
                    pass
                _out.append(_e)
        _walk(_t.parmTemplateGroup().entries())
        _info["parms"] = _out
    result = _info
''')


@mcp.tool()
def get_hou_help(name: str) -> str:
    """Returns the Python docs for a hou module member, e.g. "Node", "SopNode.geometry",
    "Geometry.createPoint" or "hipFile"."""
    return bridge.python(rf'''
import pydoc
_obj = hou
for _part in {name!r}.replace("hou.", "", 1).split("."):
    _obj = getattr(_obj, _part)
result = pydoc.render_doc(_obj, renderer=pydoc.plaintext)[:20000]
''')


@mcp.tool()
def find_node_errors(root: str = "/", include_warnings: bool = False, limit: int = 200) -> str:
    """Lists nodes under `root` that have cook errors (and optionally warnings), with messages.

    Only reports nodes that have already cooked; it does not force a cook.
    """
    return bridge.python(rf'''
_root = hou.node({root!r})
if _root is None:
    raise ValueError("No node at %s" % {root!r})
_found = []
for _n in [_root] + list(_root.allSubChildren(recurse_in_locked_nodes=False)):
    try:
        _errs = list(_n.errors())
        _warns = list(_n.warnings()) if {include_warnings!r} else []
    except Exception:
        continue
    if _errs or _warns:
        _e = {{"path": _n.path(), "type": _n.type().name()}}
        if _errs:
            _e["errors"] = _errs
        if _warns:
            _e["warnings"] = _warns
        _found.append(_e)
result = {{"count": len(_found), "nodes": _found[:{int(limit)}]}}
''')


@mcp.tool()
def capture_viewport(width: int = 960, height: int = 540, camera: str = "") -> Image:
    """Takes a screenshot (flipbook of the current frame) of the Houdini Scene View.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        camera: Optional camera node path to look through for the capture (restored afterwards).
    """
    return bridge.image({"cmd": "capture", "width": width, "height": height,
                         "camera": camera or None})


def main() -> None:
    import sys
    args = sys.argv[1:]
    if args and args[0] in ("install", "uninstall"):
        import argparse
        from houdini_mcp_bridge import install as installer
        parser = argparse.ArgumentParser(prog=f"houdini-mcp-bridge {args[0]}")
        parser.add_argument("--prefs-dir", action="append", default=[],
                            help="Houdini prefs folder (e.g. .../houdini21.0). Repeatable. "
                                 "Default: every one found.")
        opts = parser.parse_args(args[1:])
        (installer.install if args[0] == "install" else installer.uninstall)(opts.prefs_dir)
        return
    if args and args[0] in ("--version", "-V"):
        print(__version__)
        return
    mcp.run()


if __name__ == "__main__":
    main()
