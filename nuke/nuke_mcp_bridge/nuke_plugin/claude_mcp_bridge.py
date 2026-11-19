# -*- coding: utf-8 -*-
"""
Claude MCP Bridge - runs inside Nuke.

Opens a small JSON-over-TCP server inside Nuke (localhost only) so the external
`nuke-mcp-bridge` MCP server can run Python and TCL in this Nuke session on Claude's behalf.

The plug-in folder installed by `nuke-mcp-bridge install` starts it automatically in
graphical Nuke (from menu.py, so never in terminal mode or render jobs). By hand:
    import claude_mcp_bridge; claude_mcp_bridge.start()   # or .stop()

Each Nuke takes the first free port from 7577 upward (NUKE_MCP_PORT sets the first port,
NUKE_MCP_PORT_RANGE how many to try), so several Nukes can run at once.
Set NUKE_MCP_AUTOSTART=0 to stop it starting with Nuke.
Works with Python 2.7 (Nuke 12) and Python 3 (Nuke 13+).

Wire protocol: one JSON object per line (newline delimited) each way.
    request : {"id": 1, "cmd": "python", "code": "...", "undo": true}
    response: {"id": 1, "ok": true, "result": ..., "stdout": "...", "error": null}
"""
from __future__ import print_function

import ast
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import traceback

try:  # Python 3
    import socketserver
    import queue as _queue_mod
    from io import StringIO
except ImportError:  # Python 2
    import SocketServer as socketserver
    import Queue as _queue_mod
    from StringIO import StringIO

try:
    import nuke
    HAS_NUKE = True
except ImportError:  # allows the bridge to be imported outside Nuke
    nuke = None
    HAS_NUKE = False

BRIDGE_VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("NUKE_MCP_PORT", "7577"))       # first port tried
PORT_RANGE = int(os.environ.get("NUKE_MCP_PORT_RANGE", "10"))     # ports tried after it
MAX_STDOUT = 200000  # chars of captured output returned per call
LOG_PREFIX = "[Claude MCP]"

_server = None
_server_thread = None


# ----------------------------------------------------------------------------
# Execution namespace
# ----------------------------------------------------------------------------
def _fresh_namespace():
    ns = {"__name__": "__claude__", "__builtins__": __builtins__}
    if HAS_NUKE:
        ns["nuke"] = nuke
        try:
            import nukescripts
            ns["nukescripts"] = nukescripts
        except Exception:
            pass
    return ns


_namespace = _fresh_namespace()


# ----------------------------------------------------------------------------
# JSON-safe conversion of arbitrary Nuke return values
# ----------------------------------------------------------------------------
try:
    _string_types = (str, unicode)  # noqa: F821  (py2)
except NameError:
    _string_types = (str,)


def _native(value):
    """Python 2 (Nuke 12): JSON gives unicode, but nuke.tcl/toNode want byte strings."""
    if sys.version_info[0] == 2 and value is not None and not isinstance(value, str):
        return value.encode("utf-8")
    return value


def _to_jsonable(value, depth=0):
    if depth > 20:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float) + _string_types):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return dict((str(k), _to_jsonable(v, depth + 1)) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(v, depth + 1) for v in value]
    if HAS_NUKE:
        if isinstance(value, nuke.Node):
            try:
                return value.fullName()
            except Exception:
                return repr(value)
        if isinstance(value, nuke.Knob):
            try:
                return value.node().fullName() + "." + value.name()
            except Exception:
                return value.name()
        if isinstance(value, nuke.Format):
            return {"name": value.name(), "width": value.width(), "height": value.height(),
                    "pixel_aspect": value.pixelAspect()}
        if isinstance(value, nuke.Box):
            return {"x": value.x(), "y": value.y(), "r": value.r(), "t": value.t()}
    module = type(value).__module__ or ""
    if module.startswith("_nuke") or module.startswith("nuke"):
        try:
            return [_to_jsonable(v, depth + 1) for v in value]
        except TypeError:
            pass
    return repr(value)


# ----------------------------------------------------------------------------
# Command handlers (always run on Nuke's main thread)
# ----------------------------------------------------------------------------
class _Tee(object):
    """Writes to the captured buffer and the original stream (Script Editor)."""

    def __init__(self, buf, original):
        self._buf = buf
        self._original = original

    def write(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        try:
            self._buf.write(text)
        except TypeError:  # py2 StringIO given a str
            self._buf.write(text.decode("utf-8", "replace"))
        try:
            self._original.write(text)
        except Exception:
            pass

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass


def _run_python(code, reset=False):
    global _namespace
    if reset:
        _namespace = _fresh_namespace()
    ns = _namespace
    ns.pop("result", None)

    tree = ast.parse(code, "<claude>", "exec")
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)
        ast.fix_missing_locations(last_expr)

    exec(compile(tree, "<claude>", "exec"), ns)
    if last_expr is not None:
        return eval(compile(last_expr, "<claude>", "eval"), ns)
    return ns.get("result")


def _run_tcl(code):
    return nuke.tcl(code)


def _find_node(name):
    node = nuke.toNode(name)
    if node is None:
        raise ValueError("No node named %r" % name)
    return node


def _default_capture_node():
    """The node shown in the active Viewer, else the first selected node."""
    viewer = None
    if nuke.GUI:
        try:
            viewer = nuke.activeViewer()
        except Exception:
            viewer = None
    if viewer is not None:
        vnode = viewer.node()
        idx = viewer.activeInput()
        if idx is not None and vnode.input(idx) is not None:
            return vnode.input(idx)
    sel = nuke.selectedNodes()
    if sel:
        return sel[0]
    raise ValueError("No node to render: nothing is shown in a Viewer or selected. Pass node=...")


def _capture(node_name=None, frame=None, width=960):
    """Renders one frame of a node's output to a PNG (via temporary Reformat + Write nodes)."""
    source = _find_node(node_name) if node_name else _default_capture_node()
    if frame is None:
        frame = nuke.frame()
    frame = int(frame)
    tmp_dir = tempfile.mkdtemp(prefix="claude_nuke_")
    path = os.path.join(tmp_dir, "capture.png").replace("\\", "/")
    created = []
    nuke.Undo.disable()
    try:
        with nuke.root():
            src_width = source.width() or nuke.root().format().width()
            reformat = nuke.nodes.Reformat(type="scale", scale=float(width) / src_width)
            created.append(reformat)
            reformat.setInput(0, source)
            write = nuke.nodes.Write(file=path, file_type="png", channels="rgb")
            created.append(write)
            write.setInput(0, reformat)
            nuke.execute(write, frame, frame)
        written = [f for f in os.listdir(tmp_dir) if f.lower().endswith(".png")]
        if not written:
            raise RuntimeError("The render did not write an image.")
        with open(os.path.join(tmp_dir, written[0]), "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
    finally:
        for n in reversed(created):
            try:
                nuke.delete(n)
            except Exception:
                pass
        nuke.Undo.enable()
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"format": "png", "base64": data, "node": source.fullName(), "frame": frame}


# ----------------------------------------------------------------------------
# Session info (answered from the socket thread, so it works while Nuke is busy)
# ----------------------------------------------------------------------------
_state = {"script": "Untitled", "busy": False, "nuke_version": None, "product": None,
          "gui": None}


def _refresh_script(*_args):
    """Main thread only (called from script callbacks and after each request)."""
    if HAS_NUKE:
        _state["script"] = nuke.root()["name"].value() or "Untitled"


def _product():
    env = nuke.env
    if env.get("ple"):
        return "Nuke Non-commercial"
    if env.get("indie"):
        return "Nuke Indie"
    if env.get("studio"):
        return "Nuke Studio"
    if env.get("nukex"):
        return "NukeX"
    return "Nuke"


def _session_info():
    return {
        "app": "nuke",
        "bridge_version": BRIDGE_VERSION,
        "pid": os.getpid(),
        "port": _server.server_address[1] if _server is not None else None,
        "nuke_version": _state["nuke_version"],
        "product": _state["product"],
        "python": sys.version.split()[0],
        "gui": _state["gui"],
        "script": _state["script"],
        "busy": _state["busy"],
    }


def _handle(request):
    """Executes one request. Runs on the main thread."""
    cmd = request.get("cmd")
    buf = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(buf, old_out)
    sys.stderr = _Tee(buf, old_err)
    use_undo = bool(request.get("undo", True)) and HAS_NUKE and cmd in ("python", "tcl")
    if use_undo:
        nuke.Undo.begin("Claude MCP")
    _state["busy"] = True
    try:
        if cmd == "python":
            value = _run_python(_native(request.get("code", "")), reset=bool(request.get("reset")))
        elif cmd == "tcl":
            value = _run_tcl(_native(request.get("code", "")))
        elif cmd == "capture":
            value = _capture(_native(request.get("node")), request.get("frame"),
                             request.get("width", 960))
        else:
            raise ValueError("Unknown cmd: %r" % (cmd,))
        ok, error = True, None
    except Exception:
        value, ok, error = None, False, traceback.format_exc()
    finally:
        _state["busy"] = False
        if use_undo:
            nuke.Undo.end()
        sys.stdout, sys.stderr = old_out, old_err
        try:
            _refresh_script()
        except Exception:
            pass

    stdout = buf.getvalue()
    if len(stdout) > MAX_STDOUT:
        stdout = stdout[:MAX_STDOUT] + "\n... [output truncated, %d chars total]" % len(stdout)
    try:
        result = _to_jsonable(value)
    except Exception:
        result = repr(value)
    return {"ok": ok, "result": result, "stdout": stdout, "error": error}


_batch_queue = _queue_mod.Queue()


def _handle_on_main_thread(request):
    if HAS_NUKE and nuke.GUI:
        return nuke.executeInMainThreadWithResult(_handle, args=(request,))
    if HAS_NUKE:
        # Terminal mode: hand the request to serve_batch() on the main thread and wait.
        done, slot = threading.Event(), []
        _batch_queue.put((request, done, slot))
        done.wait()
        return slot[0]
    return _handle(request)


def serve_batch(poll_seconds=0.1):
    """For terminal mode (nuke -t): call from the main thread after start(); runs requests until stop()."""
    while _server is not None:
        try:
            request, done, slot = _batch_queue.get(timeout=poll_seconds)
        except _queue_mod.Empty:
            continue
        try:
            slot.append(_handle(request))
        except Exception:
            slot.append({"ok": False, "result": None, "stdout": "", "error": traceback.format_exc()})
        done.set()


# ----------------------------------------------------------------------------
# TCP server
# ----------------------------------------------------------------------------
class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req_id = None
            try:
                request = json.loads(line.decode("utf-8"))
                req_id = request.get("id")
                if request.get("cmd") == "ping":
                    response = {"ok": True, "result": _session_info(), "stdout": "", "error": None}
                else:
                    response = _handle_on_main_thread(request)
            except Exception:
                response = {"ok": False, "result": None, "stdout": "",
                            "error": traceback.format_exc()}
            response["id"] = req_id
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()


_IS_WINDOWS = sys.platform.startswith("win")


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, so two Nukes
    # would silently share one. Claim the port exclusively there instead.
    allow_reuse_address = not _IS_WINDOWS
    daemon_threads = True

    def server_bind(self):
        if _IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)


_callbacks_added = [False]


def start(port=None, host=DEFAULT_HOST):
    """Starts the bridge server (no-op if already running). Call from Nuke's main thread.

    With no port, takes the first free port in NUKE_MCP_PORT .. NUKE_MCP_PORT + NUKE_MCP_PORT_RANGE - 1
    so several Nukes can run side by side.
    """
    global _server, _server_thread
    if _server is not None:
        return _server.server_address[1]
    ports = [int(port)] if port is not None else list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_RANGE))
    last_error = None
    for candidate in ports:
        try:
            _server = _Server((host, candidate), _RequestHandler)
            break
        except (socket.error, OSError) as exc:
            last_error = exc
    else:
        raise RuntimeError("No free port in %d-%d (%s)" % (ports[0], ports[-1], last_error))

    if HAS_NUKE:
        _state["nuke_version"] = nuke.NUKE_VERSION_STRING
        _state["product"] = _product()
        _state["gui"] = bool(nuke.GUI)
        _refresh_script()
        if not _callbacks_added[0]:
            nuke.addOnScriptLoad(_refresh_script, nodeClass="Root")
            nuke.addOnScriptSave(_refresh_script, nodeClass="Root")
            nuke.addOnScriptClose(_refresh_script, nodeClass="Root")
            _callbacks_added[0] = True
    _server_thread = threading.Thread(target=_server.serve_forever, name="ClaudeMCPBridge")
    _server_thread.daemon = True
    _server_thread.start()
    bound = _server.server_address[1]
    print("%s Bridge listening on %s:%d (pid %d)" % (LOG_PREFIX, host, bound, os.getpid()))
    return bound


def stop():
    """Stops the bridge server."""
    global _server, _server_thread
    if _server is None:
        return
    _server.shutdown()
    _server.server_close()
    _server = None
    _server_thread = None
    print("%s Bridge stopped" % LOG_PREFIX)


def is_running():
    return _server is not None


def autostart():
    """Called from menu.py (graphical Nuke only)."""
    if not HAS_NUKE or os.environ.get("NUKE_MCP_AUTOSTART", "1") == "0" or not nuke.GUI:
        return
    try:
        start()
    except Exception as exc:
        sys.stderr.write("%s Could not start bridge: %s\n" % (LOG_PREFIX, exc))
