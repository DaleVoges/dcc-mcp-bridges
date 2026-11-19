"""
Nuke MCP server.

Exposes tools that let Claude drive a running Foundry Nuke session through the
`claude_mcp_bridge` module running inside Nuke (installed with `nuke-mcp-bridge install`).

Run (stdio transport):  uvx --from git+https://github.com/DaleVoges/nuke-mcp-bridge nuke-mcp-bridge
                        (or, from a checkout: python -m nuke_mcp_bridge)
Env vars:
    NUKE_MCP_HOST        (default 127.0.0.1)
    NUKE_MCP_PORT        first port to scan for Nukes (default 7577)
    NUKE_MCP_PORT_RANGE  how many ports to scan (default 10)
    NUKE_MCP_TIMEOUT     seconds to wait for a Nuke call (default 600)
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

from nuke_mcp_bridge import __version__

HOST = os.environ.get("NUKE_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("NUKE_MCP_PORT", "7577"))            # first port scanned
PORT_RANGE = int(os.environ.get("NUKE_MCP_PORT_RANGE", "10"))  # number of ports scanned
TIMEOUT = float(os.environ.get("NUKE_MCP_TIMEOUT", "600"))
INSTALL_COMMAND = "uvx --from git+https://github.com/DaleVoges/nuke-mcp-bridge nuke-mcp-bridge install"

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

mcp = FastMCP("nuke", instructions=INSTRUCTIONS, version=__version__) \
    if "version" in FastMCP.__init__.__code__.co_varnames else FastMCP("nuke", instructions=INSTRUCTIONS)


# ----------------------------------------------------------------------------
# Bridge client
# ----------------------------------------------------------------------------
class NukeBridgeError(ToolError):
    """Raised for Nuke-side errors; ToolError makes the message reach Claude verbatim."""


class SessionGoneError(NukeBridgeError):
    """The selected Nuke is no longer reachable (closed, or its port now belongs to another)."""


_ids = itertools.count(1)


def _exchange(fh, payload: dict[str, Any]) -> dict[str, Any]:
    fh.write((json.dumps(dict(payload, id=next(_ids))) + "\n").encode("utf-8"))
    fh.flush()
    line = fh.readline()
    if not line:
        raise ConnectionError("Nuke closed the connection")
    return json.loads(line.decode("utf-8"))


def probe(port: int) -> dict[str, Any] | None:
    """Returns the session info of the Nuke bridge on `port`, or None if nothing answers."""
    try:
        with socket.create_connection((HOST, port), timeout=0.5) as sock:
            sock.settimeout(3)
            with sock.makefile("rwb") as fh:
                resp = _exchange(fh, {"cmd": "ping"})
    except (OSError, ValueError):
        return None
    info = resp.get("result") if resp.get("ok") else None
    if not info or info.get("app") != "nuke":
        return None  # something else (e.g. a Maya or Houdini bridge) on this port
    return info


def scan() -> list[dict[str, Any]]:
    """Finds every Nuke bridge in the port range (probed in parallel)."""
    ports = range(PORT, PORT + PORT_RANGE)
    with ThreadPoolExecutor(max_workers=len(ports) or 1) as pool:
        return [info for info in pool.map(probe, ports) if info]


def _describe(sessions: list[dict[str, Any]], selected_pid: int | None = None) -> str:
    lines = []
    for s in sessions:
        mark = "  <- selected" if s.get("pid") == selected_pid else ""
        busy = "  (busy)" if s.get("busy") else ""
        mode = "" if s.get("gui") else " (terminal mode)"
        lines.append(f"- port {s['port']}: {s.get('product')} {s.get('nuke_version')}{mode}, "
                     f"pid {s['pid']}, script: {s.get('script')}{busy}{mark}")
    return "\n".join(lines)


class NukeClient:
    """Keeps one TCP connection to one Nuke bridge (identified by port + pid)."""

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
                f"Cannot reach Nuke at {self.host}:{self.port} ({exc}). It may have been closed. "
                "Call list_nuke_sessions to see which Nukes are available."
            ) from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._file = sock.makefile("rwb")
        if self.pid is not None:
            # Make sure this port still belongs to the Nuke that was selected.
            try:
                info = _exchange(self._file, {"cmd": "ping"}).get("result") or {}
            except (OSError, ValueError) as exc:
                self._close()
                raise SessionGoneError(f"Nuke on port {self.port} stopped answering ({exc}).") from exc
            if info.get("pid") != self.pid:
                self._close()
                raise SessionGoneError(
                    f"The selected Nuke (pid {self.pid}) is gone; port {self.port} now belongs to "
                    f"pid {info.get('pid')} with script {info.get('script')}. Nothing was run. "
                    "Call list_nuke_sessions and confirm with the user which Nuke to use."
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
                    raise NukeBridgeError(
                        f"Nuke did not answer within {self.timeout:.0f}s "
                        "(it may still be busy running the command)."
                    ) from exc
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 1:
                        raise SessionGoneError("Lost connection to Nuke.")
            raise NukeBridgeError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._close()


class Sessions:
    """Routes calls to the selected Nuke; auto-selects only when exactly one is running."""

    def __init__(self) -> None:
        self.current: NukeClient | None = None
        self._lock = threading.Lock()

    def select(self, info: dict[str, Any]) -> None:
        with self._lock:
            if self.current is not None:
                self.current.close()
            self.current = NukeClient(HOST, info["port"], TIMEOUT, info["pid"])

    def _client(self) -> NukeClient:
        if self.current is not None:
            return self.current
        found = scan()
        if not found:
            raise NukeBridgeError(
                f"No Nuke found on ports {PORT}-{PORT + PORT_RANGE - 1}. Ask the user to open Nuke. "
                "If it is open, the bridge may not be installed: the user installs it by running "
                f"`{INSTALL_COMMAND}` in a terminal and restarting Nuke "
                "(or starts it from Edit > Claude MCP > Start Bridge)."
            )
        if len(found) > 1:
            raise NukeBridgeError(
                f"{len(found)} Nuke sessions are running and none is selected yet, so nothing "
                f"was run:\n{_describe(found)}\n"
                "Ask the user which one to use, then call select_nuke_session(port=...)."
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


nuke_sessions = Sessions()


def _format(resp: dict[str, Any]) -> str:
    """Turns a bridge response into text for Claude; raises on Nuke-side errors."""
    parts = []
    if resp.get("stdout"):
        parts.append("stdout:\n" + resp["stdout"].rstrip())
    if not resp.get("ok"):
        parts.append("error:\n" + (resp.get("error") or "unknown error").rstrip())
        raise NukeBridgeError("\n\n".join(parts))
    result = resp.get("result")
    if result is not None:
        if isinstance(result, str) and "\n" in result:
            parts.append("result:\n" + result.rstrip())
        else:
            parts.append("result:\n" + json.dumps(result, indent=2, ensure_ascii=False))
    return "\n\n".join(parts) or "OK (no output)"


def _python(code: str) -> str:
    return _format(nuke_sessions.call({"cmd": "python", "code": code, "undo": False}))


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
    resp = nuke_sessions.call({"cmd": "ping"})
    text = _format(resp)
    bridge_version = (resp.get("result") or {}).get("bridge_version")
    if bridge_version != __version__:
        text += (f"\n\nNote: the bridge inside Nuke is version {bridge_version} but this server is "
                 f"{__version__}. Tell the user to update it by running `{INSTALL_COMMAND}` and "
                 "restarting Nuke.")
    return text


@mcp.tool()
def list_nuke_sessions() -> str:
    """Lists every running Nuke with the bridge started (port, version, pid, open script).

    When several are running, ask the user which one to use and call select_nuke_session.
    """
    found = scan()
    if not found:
        return f"No Nuke found on ports {PORT}-{PORT + PORT_RANGE - 1}."
    selected = nuke_sessions.current.pid if nuke_sessions.current else None
    note = "" if selected else "\n\nNo session selected yet."
    return _describe(found, selected) + note


@mcp.tool()
def select_nuke_session(port: int) -> str:
    """Chooses which Nuke the other tools talk to, by the port shown in list_nuke_sessions."""
    info = probe(port)
    if info is None:
        raise NukeBridgeError(f"No Nuke bridge answering on port {port}. Call list_nuke_sessions.")
    nuke_sessions.select(info)
    return "Selected:\n" + _describe([info], info["pid"])


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
    return _format(nuke_sessions.call({"cmd": "python", "code": code, "undo": undoable,
                                       "reset": reset_namespace}))


@mcp.tool()
def execute_tcl(command: str, undoable: bool = True) -> str:
    """Runs a TCL command or expression inside Nuke (nuke.tcl) and returns its result."""
    return _format(nuke_sessions.call({"cmd": "tcl", "code": command, "undo": undoable}))


@mcp.tool()
def get_script_info() -> str:
    """Summarises the open script: name, format, fps, frame range, colour management,
    selection, node counts by class, Read and Write nodes, and Viewer inputs."""
    return _python(r'''
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
    return _python(rf'''
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
    return _python(_KNOB_HELPERS + rf'''
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
    return _python(_KNOB_HELPERS + rf'''
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
    return _python(rf'''
import pydoc
_obj = nuke
for _part in {name!r}.replace("nuke.", "", 1).split("."):
    _obj = getattr(_obj, _part)
result = pydoc.plain(pydoc.render_doc(_obj))[:20000]
''')


@mcp.tool()
def find_node_errors(recursive: bool = True, limit: int = 200) -> str:
    """Lists nodes that are in an error state (missing files, bad expressions, invalid inputs...)."""
    return _python(rf'''
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
    resp = nuke_sessions.call({"cmd": "capture", "node": node or None, "frame": frame,
                               "width": width})
    if not resp.get("ok"):
        _format(resp)
    return Image(data=base64.b64decode(resp["result"]["base64"]), format="png")


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
