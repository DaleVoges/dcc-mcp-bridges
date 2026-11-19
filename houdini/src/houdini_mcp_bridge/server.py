"""
Houdini MCP server.

Exposes tools that let Claude drive a running SideFX Houdini session through the
`claude_mcp_bridge` module running inside Houdini (installed with `houdini-mcp-bridge install`).

Run (stdio transport):  uvx --from git+https://github.com/DaleVoges/houdini-mcp-bridge houdini-mcp-bridge
                        (or, from a checkout: python -m houdini_mcp_bridge)
Env vars:
    HOUDINI_MCP_HOST        (default 127.0.0.1)
    HOUDINI_MCP_PORT        first port to scan for Houdinis (default 7477)
    HOUDINI_MCP_PORT_RANGE  how many ports to scan (default 10)
    HOUDINI_MCP_TIMEOUT     seconds to wait for a Houdini call (default 600)
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

from houdini_mcp_bridge import __version__

HOST = os.environ.get("HOUDINI_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOUDINI_MCP_PORT", "7477"))            # first port scanned
PORT_RANGE = int(os.environ.get("HOUDINI_MCP_PORT_RANGE", "10"))  # number of ports scanned
TIMEOUT = float(os.environ.get("HOUDINI_MCP_TIMEOUT", "600"))
INSTALL_COMMAND = ("uvx --from git+https://github.com/DaleVoges/houdini-mcp-bridge "
                   "houdini-mcp-bridge install")

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

mcp = FastMCP("houdini", instructions=INSTRUCTIONS, version=__version__) \
    if "version" in FastMCP.__init__.__code__.co_varnames else FastMCP("houdini", instructions=INSTRUCTIONS)


# ----------------------------------------------------------------------------
# Bridge client
# ----------------------------------------------------------------------------
class HoudiniBridgeError(ToolError):
    """Raised for Houdini-side errors; ToolError makes the message reach Claude verbatim."""


class SessionGoneError(HoudiniBridgeError):
    """The selected Houdini is no longer reachable (closed, or its port now belongs to another)."""


_ids = itertools.count(1)


def _exchange(fh, payload: dict[str, Any]) -> dict[str, Any]:
    fh.write((json.dumps(dict(payload, id=next(_ids))) + "\n").encode("utf-8"))
    fh.flush()
    line = fh.readline()
    if not line:
        raise ConnectionError("Houdini closed the connection")
    return json.loads(line.decode("utf-8"))


def probe(port: int) -> dict[str, Any] | None:
    """Returns the session info of the Houdini bridge on `port`, or None if nothing answers."""
    try:
        with socket.create_connection((HOST, port), timeout=0.5) as sock:
            sock.settimeout(3)
            with sock.makefile("rwb") as fh:
                resp = _exchange(fh, {"cmd": "ping"})
    except (OSError, ValueError):
        return None
    info = resp.get("result") if resp.get("ok") else None
    if not info or info.get("app") != "houdini":
        return None  # something else (e.g. a Maya bridge) on this port
    return info


def scan() -> list[dict[str, Any]]:
    """Finds every Houdini bridge in the port range (probed in parallel)."""
    ports = range(PORT, PORT + PORT_RANGE)
    with ThreadPoolExecutor(max_workers=len(ports) or 1) as pool:
        return [info for info in pool.map(probe, ports) if info]


def _describe(sessions: list[dict[str, Any]], selected_pid: int | None = None) -> str:
    lines = []
    for s in sessions:
        mark = "  <- selected" if s.get("pid") == selected_pid else ""
        busy = "  (busy)" if s.get("busy") else ""
        mode = "" if s.get("ui") else " (no UI)"
        lines.append(f"- port {s['port']}: {s.get('product')} {s.get('houdini_version')}{mode}, "
                     f"pid {s['pid']}, scene: {s.get('scene')}{busy}{mark}")
    return "\n".join(lines)


class HoudiniClient:
    """Keeps one TCP connection to one Houdini bridge (identified by port + pid)."""

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
                f"Cannot reach Houdini at {self.host}:{self.port} ({exc}). It may have been closed. "
                "Call list_houdini_sessions to see which Houdinis are available."
            ) from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._file = sock.makefile("rwb")
        if self.pid is not None:
            # Make sure this port still belongs to the Houdini that was selected.
            try:
                info = _exchange(self._file, {"cmd": "ping"}).get("result") or {}
            except (OSError, ValueError) as exc:
                self._close()
                raise SessionGoneError(
                    f"Houdini on port {self.port} stopped answering ({exc}).") from exc
            if info.get("pid") != self.pid:
                self._close()
                raise SessionGoneError(
                    f"The selected Houdini (pid {self.pid}) is gone; port {self.port} now belongs to "
                    f"pid {info.get('pid')} with scene {info.get('scene')}. Nothing was run. "
                    "Call list_houdini_sessions and confirm with the user which Houdini to use."
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
                    raise HoudiniBridgeError(
                        f"Houdini did not answer within {self.timeout:.0f}s "
                        "(it may still be busy running the command)."
                    ) from exc
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 1:
                        raise SessionGoneError("Lost connection to Houdini.")
            raise HoudiniBridgeError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._close()


class Sessions:
    """Routes calls to the selected Houdini; auto-selects only when exactly one is running."""

    def __init__(self) -> None:
        self.current: HoudiniClient | None = None
        self._lock = threading.Lock()

    def select(self, info: dict[str, Any]) -> None:
        with self._lock:
            if self.current is not None:
                self.current.close()
            self.current = HoudiniClient(HOST, info["port"], TIMEOUT, info["pid"])

    def _client(self) -> HoudiniClient:
        if self.current is not None:
            return self.current
        found = scan()
        if not found:
            raise HoudiniBridgeError(
                f"No Houdini found on ports {PORT}-{PORT + PORT_RANGE - 1}. Ask the user to open "
                "Houdini. If it is open, the bridge may not be installed: the user installs it by "
                f"running `{INSTALL_COMMAND}` in a terminal and restarting Houdini."
            )
        if len(found) > 1:
            raise HoudiniBridgeError(
                f"{len(found)} Houdini sessions are running and none is selected yet, so nothing "
                f"was run:\n{_describe(found)}\n"
                "Ask the user which one to use, then call select_houdini_session(port=...)."
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


houdini = Sessions()


def _format(resp: dict[str, Any]) -> str:
    """Turns a bridge response into text for Claude; raises on Houdini-side errors."""
    parts = []
    if resp.get("stdout"):
        parts.append("stdout:\n" + resp["stdout"].rstrip())
    if not resp.get("ok"):
        parts.append("error:\n" + (resp.get("error") or "unknown error").rstrip())
        raise HoudiniBridgeError("\n\n".join(parts))
    result = resp.get("result")
    if result is not None:
        if isinstance(result, str) and "\n" in result:
            parts.append("result:\n" + result.rstrip())
        else:
            parts.append("result:\n" + json.dumps(result, indent=2, ensure_ascii=False))
    return "\n\n".join(parts) or "OK (no output)"


def _python(code: str) -> str:
    return _format(houdini.call({"cmd": "python", "code": code, "undo": False}))


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@mcp.tool()
def houdini_status() -> str:
    """Reports the selected Houdini: version, product, license, Python version, port, pid, .hip file."""
    resp = houdini.call({"cmd": "ping"})
    text = _format(resp)
    bridge_version = (resp.get("result") or {}).get("bridge_version")
    if bridge_version != __version__:
        text += (f"\n\nNote: the bridge inside Houdini is version {bridge_version} but this server "
                 f"is {__version__}. Tell the user to update it by running `{INSTALL_COMMAND}` "
                 "and restarting Houdini.")
    return text


@mcp.tool()
def list_houdini_sessions() -> str:
    """Lists every running Houdini with the bridge started (port, version, pid, open .hip file).

    When several are running, ask the user which one to use and call select_houdini_session.
    """
    found = scan()
    if not found:
        return f"No Houdini found on ports {PORT}-{PORT + PORT_RANGE - 1}."
    selected = houdini.current.pid if houdini.current else None
    note = "" if selected else "\n\nNo session selected yet."
    return _describe(found, selected) + note


@mcp.tool()
def select_houdini_session(port: int) -> str:
    """Chooses which Houdini the other tools talk to, by the port shown in list_houdini_sessions."""
    info = probe(port)
    if info is None:
        raise HoudiniBridgeError(
            f"No Houdini bridge answering on port {port}. Call list_houdini_sessions.")
    houdini.select(info)
    return "Selected:\n" + _describe([info], info["pid"])


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
    return _format(houdini.call({"cmd": "python", "code": code, "undo": undoable,
                                 "reset": reset_namespace}))


@mcp.tool()
def execute_hscript(command: str, undoable: bool = True) -> str:
    """Runs an HScript command inside Houdini (hou.hscript) and returns its output."""
    return _format(houdini.call({"cmd": "hscript", "code": command, "undo": undoable}))


@mcp.tool()
def get_scene_info() -> str:
    """Summarises the open scene: .hip file, frame range, fps, selection, node counts per context."""
    return _python(r'''
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
    return _python(rf'''
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
    return _python(rf'''
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
    return _python(rf'''
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
    return _python(rf'''
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
    return _python(rf'''
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
    resp = houdini.call({"cmd": "capture", "width": width, "height": height,
                         "camera": camera or None})
    if not resp.get("ok"):
        _format(resp)
    return Image(data=base64.b64decode(resp["result"]["base64"]), format="png")


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
