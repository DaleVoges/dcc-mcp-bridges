# -*- coding: utf-8 -*-
"""
Claude MCP Bridge - Maya plug-in.

Opens a small JSON-over-TCP server inside Maya (localhost only) so the
external `maya_mcp_server.py` MCP server can run Python (maya.cmds / PyMEL /
OpenMaya) and MEL inside this Maya session on Claude's behalf.

Load it from the Plug-in Manager, or:
    cmds.loadPlugin("claude_mcp_bridge.py")

Each Maya takes the first free port from 7377 upward (MAYA_MCP_PORT sets the first
port, MAYA_MCP_PORT_RANGE how many to try), so several Mayas can run at once.
Works with Python 2.7 (Maya 2018-2020) and Python 3 (Maya 2022+).

Wire protocol: one JSON object per line (newline delimited) each way.
    request : {"id": 1, "cmd": "python", "code": "...", "undo": true}
    response: {"id": 1, "ok": true, "result": ..., "stdout": "...", "error": null}
"""
from __future__ import print_function

import ast
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import traceback

try:  # Python 3
    import socketserver
    from io import StringIO
except ImportError:  # Python 2
    import SocketServer as socketserver
    from StringIO import StringIO

try:
    import maya.cmds as cmds
    import maya.mel as mel
    import maya.utils as maya_utils
    import maya.api.OpenMaya as om
    import maya.OpenMaya as om1
    HAS_MAYA = True
except ImportError:  # allows the bridge to be unit tested outside Maya
    cmds = mel = maya_utils = om = om1 = None
    HAS_MAYA = False

PLUGIN_VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("MAYA_MCP_PORT", "7377"))        # first port tried
PORT_RANGE = int(os.environ.get("MAYA_MCP_PORT_RANGE", "10"))      # ports tried after it
MAX_STDOUT = 200000  # chars of captured output returned per call

_server = None
_server_thread = None


def maya_useNewAPI():
    """Tells Maya this plug-in uses the Python API 2.0."""
    pass


# ----------------------------------------------------------------------------
# Execution namespace
# ----------------------------------------------------------------------------
class _LazyPyMel(object):
    """Imports pymel on first attribute access (it is slow and optional in 2022+)."""

    _mod = None

    def __getattr__(self, name):
        if _LazyPyMel._mod is None:
            try:
                import pymel.core
            except ImportError:
                raise ImportError(
                    "PyMEL is not installed in this Maya. Use maya.cmds/OpenMaya instead, or "
                    "install it (Maya 2022+: mayapy -m pip install pymel, or the Maya installer's "
                    "PyMEL option).")
            _LazyPyMel._mod = pymel.core
        return getattr(_LazyPyMel._mod, name)

    def __repr__(self):
        return "<lazy pymel.core>"


def _fresh_namespace():
    ns = {"__name__": "__claude__", "__builtins__": __builtins__}
    if HAS_MAYA:
        ns.update({
            "cmds": cmds,
            "mc": cmds,
            "mel": mel,
            "om": om,      # maya.api.OpenMaya (API 2.0)
            "om2": om,
            "om1": om1,    # maya.OpenMaya (API 1.0)
            "pm": _LazyPyMel(),
        })
        try:
            import maya.api.OpenMayaAnim as oma
            ns["oma"] = oma
        except ImportError:
            pass
    return ns


_namespace = _fresh_namespace()


# ----------------------------------------------------------------------------
# JSON-safe conversion of arbitrary Maya return values
# ----------------------------------------------------------------------------
try:
    _string_types = (str, unicode)  # noqa: F821  (py2)
except NameError:
    _string_types = (str,)


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
    # OpenMaya arrays/vectors/matrices are iterable; PyMEL nodes stringify nicely.
    module = type(value).__module__ or ""
    if module.startswith("pymel"):
        try:
            if hasattr(value, "__iter__") and not hasattr(value, "name"):
                return [_to_jsonable(v, depth + 1) for v in value]
        except Exception:
            pass
        return str(value)
    if module.startswith("OpenMaya") or module.startswith("maya"):
        try:
            return [_to_jsonable(v, depth + 1) for v in value]
        except Exception:
            return str(value)
    return repr(value)


# ----------------------------------------------------------------------------
# Command handlers (always run on Maya's main thread)
# ----------------------------------------------------------------------------
class _Tee(object):
    """Writes to the captured buffer and the original stream (Script Editor)."""

    def __init__(self, buf, original):
        self._buf = buf
        self._original = original

    def write(self, text):
        self._buf.write(text if not isinstance(text, bytes) else text.decode("utf-8", "replace"))
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


def _run_mel(code):
    return mel.eval(code)


def _capture_viewport(width=960, height=540, camera=None):
    """Playblasts the current frame of the active (or given camera's) viewport."""
    panel = cmds.getPanel(withFocus=True)
    if not panel or cmds.getPanel(typeOf=panel) != "modelPanel":
        panels = cmds.getPanel(visiblePanels=True) or []
        model_panels = [p for p in panels if cmds.getPanel(typeOf=p) == "modelPanel"]
        panel = model_panels[0] if model_panels else None
    if panel is None:
        raise RuntimeError("No visible model panel to capture (is Maya running in batch mode?)")

    previous_cam = None
    if camera:
        previous_cam = cmds.modelPanel(panel, query=True, camera=True)
        cmds.modelPanel(panel, edit=True, camera=camera)
    cmds.setFocus(panel)

    fd, path = tempfile.mkstemp(prefix="claude_viewport_", suffix=".png")
    os.close(fd)
    frame = cmds.currentTime(query=True)
    try:
        out = cmds.playblast(
            frame=[frame], format="image", compression="png", completeFilename=path,
            viewer=False, showOrnaments=True, offScreen=True, percent=100,
            widthHeight=[int(width), int(height)], forceOverwrite=True, clearCache=True,
        )
        out = out or path
        with open(out, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
    finally:
        if previous_cam:
            cmds.modelPanel(panel, edit=True, camera=previous_cam)
        try:
            os.remove(path)
        except OSError:
            pass
    return {"format": "png", "base64": data, "panel": panel,
            "camera": cmds.modelPanel(panel, query=True, camera=True)}


# ----------------------------------------------------------------------------
# Session info (answered from the socket thread, so it works while Maya is busy)
# ----------------------------------------------------------------------------
_state = {"scene": "untitled", "busy": False, "maya_version": None, "batch": None}
_callback_ids = []


def _refresh_scene(*_args):
    """Main thread only (called from scene callbacks and after each request)."""
    if HAS_MAYA:
        _state["scene"] = cmds.file(query=True, sceneName=True) or "untitled"


def _session_info():
    return {
        "bridge_version": PLUGIN_VERSION,
        "pid": os.getpid(),
        "port": _server.server_address[1] if _server is not None else None,
        "maya_version": _state["maya_version"],
        "python": sys.version.split()[0],
        "batch": _state["batch"],
        "scene": _state["scene"],
        "busy": _state["busy"],
    }


def _handle(request):
    """Executes one request. Runs on the main thread."""
    cmd = request.get("cmd")
    buf = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(buf, old_out)
    sys.stderr = _Tee(buf, old_err)
    use_undo = bool(request.get("undo", True)) and HAS_MAYA and cmd in ("python", "mel")
    if use_undo:
        cmds.undoInfo(openChunk=True, chunkName="claude_mcp")
    _state["busy"] = True
    try:
        if cmd == "python":
            value = _run_python(request.get("code", ""), reset=bool(request.get("reset")))
        elif cmd == "mel":
            value = _run_mel(request.get("code", ""))
        elif cmd == "capture":
            value = _capture_viewport(request.get("width", 960), request.get("height", 540),
                                      request.get("camera"))
        else:
            raise ValueError("Unknown cmd: %r" % (cmd,))
        ok, error = True, None
    except Exception:
        value, ok, error = None, False, traceback.format_exc()
    finally:
        _state["busy"] = False
        if use_undo:
            cmds.undoInfo(closeChunk=True)
        sys.stdout, sys.stderr = old_out, old_err
        try:
            _refresh_scene()
        except Exception:
            pass

    stdout = buf.getvalue()
    if len(stdout) > MAX_STDOUT:
        stdout = stdout[:MAX_STDOUT] + "\n... [output truncated, %d chars total]" % len(stdout)
    return {"ok": ok, "result": _to_jsonable(value), "stdout": stdout, "error": error}


try:
    import queue as _queue_mod
except ImportError:
    import Queue as _queue_mod

_batch_queue = _queue_mod.Queue()
_IS_BATCH = HAS_MAYA and cmds.about(batch=True)


def _handle_on_main_thread(request):
    if HAS_MAYA and not _IS_BATCH:
        return maya_utils.executeInMainThreadWithResult(_handle, request)
    if _IS_BATCH:
        # Batch/mayapy: Maya commands misbehave off the main thread, so hand the
        # request to serve_batch() running on the main thread and wait for it.
        done, slot = threading.Event(), []
        _batch_queue.put((request, done, slot))
        done.wait()
        return slot[0]
    return _handle(request)


def serve_batch(poll_seconds=0.1):
    """For mayapy/batch mode: call from the main thread after start(); runs requests until stop()."""
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
            payload = (json.dumps(response) + "\n").encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()


_IS_WINDOWS = sys.platform.startswith("win")


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, so two Mayas
    # would silently share one. Claim the port exclusively there instead.
    allow_reuse_address = not _IS_WINDOWS
    daemon_threads = True

    def server_bind(self):
        if _IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)


def _register_scene_callbacks():
    if not HAS_MAYA:
        return
    for msg in (om.MSceneMessage.kAfterOpen, om.MSceneMessage.kAfterNew,
                om.MSceneMessage.kAfterSave):
        _callback_ids.append(om.MSceneMessage.addCallback(msg, _refresh_scene))


def _remove_scene_callbacks():
    if HAS_MAYA and _callback_ids:
        om.MMessage.removeCallbacks(_callback_ids)
    del _callback_ids[:]


def start(port=None, host=DEFAULT_HOST):
    """Starts the bridge server (no-op if already running). Call from Maya's main thread.

    With no port, takes the first free port in MAYA_MCP_PORT .. MAYA_MCP_PORT + MAYA_MCP_PORT_RANGE - 1
    so several Maya sessions can run side by side.
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

    if HAS_MAYA:
        _state["maya_version"] = cmds.about(version=True)
        _state["batch"] = bool(cmds.about(batch=True))
        _refresh_scene()
        _register_scene_callbacks()
    _server_thread = threading.Thread(target=_server.serve_forever, name="ClaudeMCPBridge")
    _server_thread.daemon = True
    _server_thread.start()
    bound = _server.server_address[1]
    print("[Claude MCP] Bridge listening on %s:%d (pid %d)" % (host, bound, os.getpid()))
    return bound


def stop():
    """Stops the bridge server."""
    global _server, _server_thread
    if _server is None:
        return
    _remove_scene_callbacks()
    _server.shutdown()
    _server.server_close()
    _server = None
    _server_thread = None
    print("[Claude MCP] Bridge stopped")


def is_running():
    return _server is not None


# ----------------------------------------------------------------------------
# Maya plug-in entry points
# ----------------------------------------------------------------------------
def initializePlugin(mobject):
    if HAS_MAYA:
        om.MFnPlugin(mobject, "Claude MCP", PLUGIN_VERSION, "Any")
    try:
        start()
    except Exception as exc:
        sys.stderr.write("[Claude MCP] Could not start bridge: %s\n" % (exc,))
        raise


def uninitializePlugin(mobject):
    stop()
