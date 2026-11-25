# -*- coding: utf-8 -*-
"""
Claude MCP Bridge - runs inside Blender.

Opens a small JSON-over-TCP server inside Blender (localhost only) so the external
`blender-mcp-bridge` MCP server can run Python (bpy) in this Blender session on Claude's
behalf.

`blender-mcp-bridge install` copies this file into each Blender version's
scripts/startup folder, so Blender loads it at startup and register() starts the bridge
in graphical Blender (not with --background, so command-line renders don't take ports).
From the Python Console:
    import claude_mcp_bridge; claude_mcp_bridge.start()   # or .stop()

Each Blender takes the first free port from 7677 upward (BLENDER_MCP_PORT sets the first
port, BLENDER_MCP_PORT_RANGE how many to try), so several Blenders can run at once.
Set BLENDER_MCP_AUTOSTART=0 to stop it starting with Blender.

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
    import bpy
    HAS_BPY = True
except ImportError:  # allows the bridge to be imported outside Blender
    bpy = None
    HAS_BPY = False

BRIDGE_VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("BLENDER_MCP_PORT", "7677"))       # first port tried
PORT_RANGE = int(os.environ.get("BLENDER_MCP_PORT_RANGE", "10"))     # ports tried after it
MAX_STDOUT = 200000  # chars of captured output returned per call
POLL_SECONDS = 0.05  # how often the main-thread timer looks for requests
LOG_PREFIX = "[Claude MCP]"

_server = None
_server_thread = None
_queue = queue.Queue()


# ----------------------------------------------------------------------------
# Helpers available to Claude's code
# ----------------------------------------------------------------------------
def view3d_override():
    """Keyword args for bpy.context.temp_override() that point at the largest 3D View.

        with bpy.context.temp_override(**view3d_override()):
            bpy.ops.view3d.view_all()

    Returns {} when there is no UI (background mode) or no 3D View is open.
    """
    if not HAS_BPY or bpy.app.background:
        return {}
    best = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            if best is None or area.width * area.height > best[1].width * best[1].height:
                best = (window, area)
    if best is None:
        return {}
    window, area = best
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    return {"window": window, "screen": window.screen, "area": area, "region": region,
            "space_data": area.spaces.active, "scene": window.scene}


# ----------------------------------------------------------------------------
# Execution namespace
# ----------------------------------------------------------------------------
def _fresh_namespace():
    ns = {"__name__": "__claude__", "__builtins__": __builtins__,
          "view3d_override": view3d_override}
    if HAS_BPY:
        import bmesh
        import mathutils
        ns.update({"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils,
                   "Vector": mathutils.Vector, "Matrix": mathutils.Matrix,
                   "Euler": mathutils.Euler, "Quaternion": mathutils.Quaternion})
    return ns


_namespace = _fresh_namespace()


# ----------------------------------------------------------------------------
# JSON-safe conversion of arbitrary bpy return values
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
    if HAS_BPY:
        import mathutils
        if isinstance(value, mathutils.Matrix):
            return [list(row) for row in value]
        if isinstance(value, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion,
                              mathutils.Color)):
            return list(value)
        # Data-blocks (objects, meshes, materials...) are most useful as their names.
        if isinstance(value, bpy.types.ID):
            return value.name
        type_name = type(value).__name__
        if type_name == "bpy_prop_collection":
            return [_to_jsonable(v, depth + 1) for v in value]
        if type_name == "bpy_prop_array":
            return [_to_jsonable(v, depth + 1) for v in value]
        if isinstance(value, bpy.types.bpy_struct):
            name = getattr(value, "name", None)
            if isinstance(name, str) and name:
                return name
            return repr(value)
    return repr(value)


# ----------------------------------------------------------------------------
# Command handlers (always run on Blender's main thread)
# ----------------------------------------------------------------------------
class _Tee(object):
    """Writes to the captured buffer and the original stream (Blender's console)."""

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
    if HAS_BPY:
        # Bound per call: bpy.data is a restricted stand-in while Blender is starting up.
        ns["C"], ns["D"] = bpy.context, bpy.data

    tree = ast.parse(code, "<claude>", "exec")
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)
        ast.fix_missing_locations(last_expr)

    exec(compile(tree, "<claude>", "exec"), ns)
    if last_expr is not None:
        return eval(compile(last_expr, "<claude>", "eval"), ns)
    return ns.get("result")


def _undo_push():
    """Records the state after a call as one undo step, so Ctrl+Z reverts the whole call."""
    if bpy.app.background:
        return
    override = view3d_override()
    if not override:
        windows = bpy.context.window_manager.windows
        override = {"window": windows[0]} if len(windows) else {}
    try:
        with bpy.context.temp_override(**override):
            bpy.ops.ed.undo_push(message="Claude MCP")
    except Exception as exc:
        sys.stderr.write("%s Undo step not recorded: %s\n" % (LOG_PREFIX, exc))


def _read_png(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _capture_viewport(width=960, height=540, camera=None):
    """Renders the 3D View (OpenGL render) to a PNG, through `camera` if given.

    Without a UI (background mode) it renders through the camera with Workbench instead.
    """
    scene = bpy.context.scene
    render = scene.render
    cam_obj = None
    if camera:
        cam_obj = bpy.data.objects.get(camera)
        if cam_obj is None or cam_obj.type != "CAMERA":
            raise ValueError("No camera object named %r" % camera)

    saved = {"x": render.resolution_x, "y": render.resolution_y,
             "pct": render.resolution_percentage, "camera": scene.camera,
             "path": render.filepath, "format": render.image_settings.file_format,
             "engine": render.engine}
    tmp_dir = tempfile.mkdtemp(prefix="claude_viewport_")
    path = os.path.join(tmp_dir, "capture.png")
    try:
        render.resolution_x, render.resolution_y = int(width), int(height)
        render.resolution_percentage = 100
        render.image_settings.file_format = "PNG"
        if cam_obj is not None:
            scene.camera = cam_obj
        if bpy.app.background:
            if scene.camera is None:
                raise RuntimeError("No viewport: Blender is running in background mode, and "
                                   "the scene has no camera to render through.")
            render.engine = "BLENDER_WORKBENCH"
            render.filepath = path
            bpy.ops.render.render(write_still=True)
            source = "workbench render through %s" % scene.camera.name
        else:
            override = view3d_override()
            if not override:
                raise RuntimeError("No 3D View is open to capture.")
            with bpy.context.temp_override(**override):
                bpy.ops.render.opengl(write_still=False, view_context=cam_obj is None)
            image = bpy.data.images.get("Render Result")
            if image is None:
                raise RuntimeError("The viewport render did not produce an image.")
            image.save_render(path, scene=scene)
            source = ("camera %s" % cam_obj.name) if cam_obj is not None else "3D View"
        if not os.path.isfile(path):
            raise RuntimeError("The capture did not write an image.")
        data = _read_png(path)
    finally:
        render.resolution_x, render.resolution_y = saved["x"], saved["y"]
        render.resolution_percentage = saved["pct"]
        render.image_settings.file_format = saved["format"]
        render.filepath = saved["path"]
        render.engine = saved["engine"]
        scene.camera = saved["camera"]
        for name in os.listdir(tmp_dir):
            try:
                os.remove(os.path.join(tmp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    return {"format": "png", "base64": data, "source": source}


# ----------------------------------------------------------------------------
# Session info (answered from the socket thread, so it works while Blender is busy)
# ----------------------------------------------------------------------------
_state = {"scene": "untitled.blend", "busy": False, "blender_version": None, "ui": None}


def _refresh_scene(*_args):
    """Main thread only (called from load/save handlers and after each request)."""
    if HAS_BPY:
        _state["scene"] = bpy.data.filepath or "untitled.blend"


def _session_info():
    return {
        "app": "blender",
        "bridge_version": BRIDGE_VERSION,
        "pid": os.getpid(),
        "port": _server.server_address[1] if _server is not None else None,
        "blender_version": _state["blender_version"],
        "python": sys.version.split()[0],
        "ui": _state["ui"],
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
    _state["busy"] = True
    try:
        if cmd == "python":
            value = _run_python(request.get("code", ""), reset=bool(request.get("reset")))
            if HAS_BPY and request.get("undo", True):
                _undo_push()
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


def _drain_queue():
    """Runs every waiting request. Main thread only."""
    while True:
        try:
            request, done, slot = _queue.get_nowait()
        except queue.Empty:
            return
        try:
            slot.append(_handle(request))
        except Exception:
            slot.append({"ok": False, "result": None, "stdout": "", "error": traceback.format_exc()})
        done.set()


def _timer():
    """bpy.app.timers callback: Blender's main thread picks up requests here."""
    if _server is None:
        return None  # unregisters the timer
    _drain_queue()
    return POLL_SECONDS


def _handle_on_main_thread(request):
    if not HAS_BPY:
        return _handle(request)
    # The socket thread hands the request to the main thread (the timer in graphical
    # Blender, serve_batch() in background mode) and waits for the answer.
    done, slot = threading.Event(), []
    _queue.put((request, done, slot))
    done.wait()
    return slot[0]


def serve_batch(poll_seconds=0.1):
    """For background mode: call from the main thread after start(); runs requests until stop()."""
    import time
    while _server is not None:
        _drain_queue()
        time.sleep(poll_seconds)


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
    # On Windows SO_REUSEADDR lets a second process bind the same port, so two Blenders
    # would silently share one. Claim the port exclusively there instead.
    allow_reuse_address = not _IS_WINDOWS
    daemon_threads = True

    def server_bind(self):
        if _IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)


def _file_handlers():
    return (bpy.app.handlers.load_post, bpy.app.handlers.save_post)


if HAS_BPY:
    from bpy.app.handlers import persistent

    @persistent
    def _on_file_event(*_args):
        _refresh_scene()
else:
    def _on_file_event(*_args):
        pass


def start(port=None, host=DEFAULT_HOST):
    """Starts the bridge server (no-op if already running). Call from Blender's main thread.

    With no port, takes the first free port in BLENDER_MCP_PORT ..
    BLENDER_MCP_PORT + BLENDER_MCP_PORT_RANGE - 1 so several Blenders can run side by side.
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

    if HAS_BPY:
        _state["blender_version"] = bpy.app.version_string
        _state["ui"] = not bpy.app.background
        try:
            _refresh_scene()
        except Exception:
            pass  # bpy.data is restricted while add-ons register; the load handler fills it in
        for handlers in _file_handlers():
            if _on_file_event not in handlers:
                handlers.append(_on_file_event)
        if not bpy.app.background and not bpy.app.timers.is_registered(_timer):
            bpy.app.timers.register(_timer, first_interval=POLL_SECONDS, persistent=True)
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
    if HAS_BPY:
        for handlers in _file_handlers():
            if _on_file_event in handlers:
                handlers.remove(_on_file_event)
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
    _server.shutdown()
    _server.server_close()
    _server = None
    _server_thread = None
    print("%s Bridge stopped" % LOG_PREFIX)


def is_running():
    return _server is not None


# Blender calls register()/unregister() for every module in scripts/startup.
def register():
    """Starts the bridge in graphical Blender only (not --background renders)."""
    if not HAS_BPY or os.environ.get("BLENDER_MCP_AUTOSTART", "1") == "0":
        return
    if bpy.app.background:
        return

    def _start():
        try:
            start()
        except Exception as exc:
            sys.stderr.write("%s Could not start bridge: %s\n" % (LOG_PREFIX, exc))
        return None

    # Defer until Blender has finished starting up and bpy.data is fully available.
    bpy.app.timers.register(_start, first_interval=0.1)


def unregister():
    stop()
