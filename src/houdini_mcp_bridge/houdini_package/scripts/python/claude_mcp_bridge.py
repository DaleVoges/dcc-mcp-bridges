# -*- coding: utf-8 -*-
"""
Claude MCP Bridge - runs inside Houdini.

Opens a small JSON-over-TCP server inside Houdini (localhost only) so the external
`houdini-mcp-bridge` MCP server can run Python (hou) and HScript in this Houdini session
on Claude's behalf.

The Houdini package installed by `houdini-mcp-bridge install` starts it automatically in
graphical Houdini (not in hython, so batch renders and PDG workers don't take ports).
From the Python Shell:
    import claude_mcp_bridge; claude_mcp_bridge.start()   # or .stop()

Each Houdini takes the first free port from 7477 upward (HOUDINI_MCP_PORT sets the first
port, HOUDINI_MCP_PORT_RANGE how many to try), so several Houdinis can run at once.
Set HOUDINI_MCP_AUTOSTART=0 to stop it starting with Houdini.

Wire protocol: one JSON object per line (newline delimited) each way.
    request : {"id": 1, "cmd": "python", "code": "...", "undo": true}
    response: {"id": 1, "ok": true, "result": ..., "stdout": "...", "error": null}
"""
import ast
import base64
import json
import os
import queue
import socket
import socketserver
import sys
import tempfile
import threading
import traceback
from io import StringIO

try:
    import hou
    HAS_HOU = True
except ImportError:  # allows the bridge to be imported outside Houdini
    hou = None
    HAS_HOU = False

BRIDGE_VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("HOUDINI_MCP_PORT", "7477"))       # first port tried
PORT_RANGE = int(os.environ.get("HOUDINI_MCP_PORT_RANGE", "10"))     # ports tried after it
MAX_STDOUT = 200000  # chars of captured output returned per call
LOG_PREFIX = "[Claude MCP]"

_server = None
_server_thread = None


# ----------------------------------------------------------------------------
# Execution namespace
# ----------------------------------------------------------------------------
def _fresh_namespace():
    ns = {"__name__": "__claude__", "__builtins__": __builtins__}
    if HAS_HOU:
        ns["hou"] = hou
    return ns


_namespace = _fresh_namespace()


# ----------------------------------------------------------------------------
# JSON-safe conversion of arbitrary hou return values
# ----------------------------------------------------------------------------
def _to_jsonable(value, depth=0):
    if depth > 20:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(v, depth + 1) for v in value]
    if HAS_HOU:
        # Nodes and parms are most useful as their paths.
        if isinstance(value, hou.NetworkMovableItem) and hasattr(value, "path"):
            return value.path()
        if isinstance(value, hou.Parm):
            return value.path()
        if isinstance(value, hou.ParmTuple):
            return value.node().path() + "/" + value.name()
        if isinstance(value, hou.EnumValue):
            return str(value)
        # Vectors, matrices, bounding boxes, colours etc. are iterable or have asTuple().
        for conv in ("asTupleOfTuples", "asTuple"):
            if hasattr(value, conv):
                try:
                    return _to_jsonable(getattr(value, conv)(), depth + 1)
                except Exception:
                    pass
        if isinstance(value, hou.BoundingBox):
            return {"min": list(value.minvec()), "max": list(value.maxvec())}
        if (type(value).__module__ or "").startswith("hou"):
            try:
                return [_to_jsonable(v, depth + 1) for v in value]
            except TypeError:
                pass
    return repr(value)


# ----------------------------------------------------------------------------
# Command handlers (always run on Houdini's main thread)
# ----------------------------------------------------------------------------
class _Tee(object):
    """Writes to the captured buffer and the original stream (Houdini console)."""

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


def _run_hscript(code):
    out, err = hou.hscript(code)
    if err.strip():
        raise RuntimeError("HScript error:\n" + err.rstrip() + ("\n\nOutput:\n" + out if out else ""))
    return out


def _scene_viewer():
    if not hou.isUIAvailable():
        raise RuntimeError("No viewport: Houdini is running without a UI (hython).")
    viewer = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.SceneViewer)
    if viewer is None:
        viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if viewer is None:
        raise RuntimeError("No Scene View pane is open to capture.")
    return viewer


def _capture_viewport(width=960, height=540, camera=None):
    """Flipbooks the current frame of the Scene View to a PNG."""
    viewer = _scene_viewer()
    viewport = viewer.curViewport()

    previous_cam = previous_view = None
    if camera:
        cam_node = hou.node(camera)
        if cam_node is None:
            raise ValueError("Camera node not found: %s" % camera)
        previous_cam = viewport.camera()
        previous_view = viewport.defaultCamera().stash()
        viewport.setCamera(cam_node)

    tmp_dir = tempfile.mkdtemp(prefix="claude_viewport_")
    path = os.path.join(tmp_dir, "capture.png").replace("\\", "/")
    frame = hou.frame()
    settings = viewer.flipbookSettings().stash()
    settings.frameRange((frame, frame))
    settings.output(path)
    settings.outputToMPlay(False)
    settings.useResolution(True)
    settings.resolution((int(width), int(height)))
    try:
        viewer.flipbook(viewport, settings)
        written = [f for f in os.listdir(tmp_dir) if f.lower().endswith(".png")]
        if not written:
            raise RuntimeError("The flipbook did not write an image.")
        with open(os.path.join(tmp_dir, written[0]), "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
    finally:
        if camera:
            if previous_cam is not None:
                viewport.setCamera(previous_cam)
            else:
                viewport.setDefaultCamera(previous_view)
        for name in os.listdir(tmp_dir):
            try:
                os.remove(os.path.join(tmp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    cam = viewport.camera()
    return {"format": "png", "base64": data, "viewport": viewport.name(),
            "camera": cam.path() if cam is not None else None}


# ----------------------------------------------------------------------------
# Session info (answered from the socket thread, so it works while Houdini is busy)
# ----------------------------------------------------------------------------
_state = {"scene": "untitled.hip", "busy": False, "houdini_version": None,
          "product": None, "license": None, "ui": None}


def _refresh_scene(*_args):
    """Main thread only (called from hip file callbacks and after each request)."""
    if HAS_HOU:
        _state["scene"] = hou.hipFile.path()


def _session_info():
    return {
        "app": "houdini",
        "bridge_version": BRIDGE_VERSION,
        "pid": os.getpid(),
        "port": _server.server_address[1] if _server is not None else None,
        "houdini_version": _state["houdini_version"],
        "product": _state["product"],
        "license": _state["license"],
        "python": sys.version.split()[0],
        "ui": _state["ui"],
        "scene": _state["scene"],
        "busy": _state["busy"],
    }


class _UndoGroup(object):
    def __init__(self, enabled):
        self._ctx = hou.undos.group("Claude MCP") if enabled else None

    def __enter__(self):
        if self._ctx is not None:
            self._ctx.__enter__()

    def __exit__(self, *exc):
        if self._ctx is not None:
            self._ctx.__exit__(*exc)
        return False


def _handle(request):
    """Executes one request. Runs on the main thread."""
    cmd = request.get("cmd")
    buf = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(buf, old_out)
    sys.stderr = _Tee(buf, old_err)
    use_undo = bool(request.get("undo", True)) and HAS_HOU and cmd in ("python", "hscript")
    _state["busy"] = True
    try:
        with _UndoGroup(use_undo):
            if cmd == "python":
                value = _run_python(request.get("code", ""), reset=bool(request.get("reset")))
            elif cmd == "hscript":
                value = _run_hscript(request.get("code", ""))
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
        sys.stdout, sys.stderr = old_out, old_err
        try:
            _refresh_scene()
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


_batch_queue = queue.Queue()


def _has_ui():
    return HAS_HOU and hou.isUIAvailable()


def _handle_on_main_thread(request):
    if _has_ui():
        import hdefereval
        return hdefereval.executeInMainThreadWithResult(_handle, request)
    if HAS_HOU:
        # hython: hand the request to serve_batch() running on the main thread and wait.
        done, slot = threading.Event(), []
        _batch_queue.put((request, done, slot))
        done.wait()
        return slot[0]
    return _handle(request)


def serve_batch(poll_seconds=0.1):
    """For hython: call from the main thread after start(); runs requests until stop()."""
    while _server is not None:
        try:
            request, done, slot = _batch_queue.get(timeout=poll_seconds)
        except queue.Empty:
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
    # On Windows SO_REUSEADDR lets a second process bind the same port, so two Houdinis
    # would silently share one. Claim the port exclusively there instead.
    allow_reuse_address = not _IS_WINDOWS
    daemon_threads = True

    def server_bind(self):
        if _IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)


def _hip_event(event_type):
    if event_type in (hou.hipFileEventType.AfterLoad, hou.hipFileEventType.AfterClear,
                      hou.hipFileEventType.AfterSave):
        _refresh_scene()


def start(port=None, host=DEFAULT_HOST):
    """Starts the bridge server (no-op if already running). Call from Houdini's main thread.

    With no port, takes the first free port in HOUDINI_MCP_PORT ..
    HOUDINI_MCP_PORT + HOUDINI_MCP_PORT_RANGE - 1 so several Houdinis can run side by side.
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
        except OSError as exc:
            last_error = exc
    else:
        raise RuntimeError("No free port in %d-%d (%s)" % (ports[0], ports[-1], last_error))

    if HAS_HOU:
        _state["houdini_version"] = hou.applicationVersionString()
        _state["product"] = hou.applicationName()
        _state["license"] = str(hou.licenseCategory()).split(".")[-1]
        _state["ui"] = hou.isUIAvailable()
        _refresh_scene()
        if _hip_event not in hou.hipFile.eventCallbacks():
            hou.hipFile.addEventCallback(_hip_event)
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
    if HAS_HOU:
        try:
            hou.hipFile.removeEventCallback(_hip_event)
        except Exception:
            pass
    _server.shutdown()
    _server.server_close()
    _server = None
    _server_thread = None
    print("%s Bridge stopped" % LOG_PREFIX)


def is_running():
    return _server is not None


def autostart():
    """Called by the package's pythonrc.py. Starts the bridge in graphical Houdini only."""
    if not HAS_HOU or os.environ.get("HOUDINI_MCP_AUTOSTART", "1") == "0":
        return
    if not hou.isUIAvailable():
        return  # hython, hbatch, render and PDG workers

    def _start():
        try:
            start()
        except Exception as exc:
            sys.stderr.write("%s Could not start bridge: %s\n" % (LOG_PREFIX, exc))

    import hdefereval
    hdefereval.executeDeferred(_start)
