"""Client side of the bridge protocol, shared by every app's MCP server.

Protocol: one JSON object per line each way. Requests carry "cmd" (ping, python, mel,
hscript, tcl, capture...) and an "id"; replies carry "ok", "result", "error" and "stdout".
A ping returns the session info: port, pid, app version, open scene, busy flag...

Env vars, with <APP> being MAYA, HOUDINI or NUKE:
    <APP>_MCP_HOST        (default 127.0.0.1)
    <APP>_MCP_PORT        first port to scan (default: the app's own, e.g. 7377 for Maya)
    <APP>_MCP_PORT_RANGE  how many ports to scan (default 10)
    <APP>_MCP_TIMEOUT     seconds to wait for a call (default 600)
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError

Session = dict[str, Any]


def create_server(name: str, instructions: str, version: str) -> FastMCP:
    """Makes the MCP server, passing `version` where the installed SDK accepts it."""
    if "version" in FastMCP.__init__.__code__.co_varnames:
        return FastMCP(name, instructions=instructions, version=version)
    return FastMCP(name, instructions=instructions)


class BridgeError(ToolError):
    """Raised for app-side errors; ToolError makes the message reach Claude verbatim."""


class SessionGoneError(BridgeError):
    """The selected session is no longer reachable (closed, or its port now belongs to another)."""


_ids = itertools.count(1)


def _exchange(fh, payload: dict[str, Any]) -> dict[str, Any]:
    fh.write((json.dumps(dict(payload, id=next(_ids))) + "\n").encode("utf-8"))
    fh.flush()
    line = fh.readline()
    if not line:
        raise ConnectionError("the app closed the connection")
    return json.loads(line.decode("utf-8"))


class _Client:
    """Keeps one TCP connection to one bridge, identified by port + pid."""

    def __init__(self, bridge: "Bridge", port: int, pid: int | None):
        self.bridge, self.port, self.pid = bridge, port, pid
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()

    def _connect(self) -> None:
        b = self.bridge
        try:
            sock = socket.create_connection((b.host, self.port), timeout=5)
        except OSError as exc:
            raise SessionGoneError(
                f"Cannot reach {b.label} at {b.host}:{self.port} ({exc}). It may have been closed. "
                f"Call {b.list_tool} to see which {b.plural} are available."
            ) from exc
        sock.settimeout(b.timeout)
        self._sock = sock
        self._file = sock.makefile("rwb")
        if self.pid is not None:
            # Make sure this port still belongs to the session that was selected.
            try:
                info = _exchange(self._file, {"cmd": "ping"}).get("result") or {}
            except (OSError, ValueError) as exc:
                self._close()
                raise SessionGoneError(
                    f"{b.label} on port {self.port} stopped answering ({exc}).") from exc
            if info.get("pid") != self.pid:
                self._close()
                raise SessionGoneError(
                    f"The selected {b.label} (pid {self.pid}) is gone; port {self.port} now belongs "
                    f"to pid {info.get('pid')} with {b.scene_key} {info.get(b.scene_key)}. Nothing "
                    f"was run. Call {b.list_tool} and confirm with the user which {b.label} to use."
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
                    raise BridgeError(
                        f"{self.bridge.label} did not answer within {self.bridge.timeout:.0f}s "
                        "(it may still be busy running the command)."
                    ) from exc
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 1:
                        raise SessionGoneError(f"Lost connection to {self.bridge.label}.")
            raise BridgeError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._close()


class Bridge:
    """Finds an app's bridges and routes calls to the selected one.

    A session is chosen automatically only when exactly one is running; with several, calls
    are refused until the user picks one, so Claude never guesses which app it is changing.

    Args:
        app: Short name used in env vars and tool names, e.g. "maya" (MAYA_MCP_PORT,
            list_maya_sessions, select_maya_session).
        label: Name shown to Claude, e.g. "Maya".
        default_port: First port scanned when <APP>_MCP_PORT is not set.
        version: This server's version, compared with the bridge's "bridge_version".
        session_label: Turns a ping result into e.g. "Maya 2027" or "houdinicore 21.0 (no UI)".
        scene_key: Ping field naming the open file ("scene", or "script" for Nuke).
        not_found_hint: Added when no session is found: how the user starts or installs one.
        update_hint: Added when the versions differ: how the user updates the app side.
        accept_missing_app: Treat a ping without an "app" field as this app (older plug-ins).
    """

    def __init__(self, app: str, label: str, default_port: int, version: str,
                 session_label: Callable[[Session], str], scene_key: str = "scene",
                 not_found_hint: str = "", update_hint: str = "",
                 accept_missing_app: bool = False):
        prefix = app.upper() + "_MCP_"
        self.app, self.label, self.plural = app, label, label + "s"
        self.version, self.session_label, self.scene_key = version, session_label, scene_key
        self.not_found_hint, self.update_hint = not_found_hint, update_hint
        self.accept_missing_app = accept_missing_app
        self.host = os.environ.get(prefix + "HOST", "127.0.0.1")
        self.port = int(os.environ.get(prefix + "PORT", str(default_port)))
        self.port_range = int(os.environ.get(prefix + "PORT_RANGE", "10"))
        self.timeout = float(os.environ.get(prefix + "TIMEOUT", "600"))
        self.list_tool = f"list_{app}_sessions"
        self.select_tool = f"select_{app}_session"
        self.current: _Client | None = None
        self._lock = threading.Lock()

    # -- discovery -------------------------------------------------------------------------
    @property
    def ports_text(self) -> str:
        return f"ports {self.port}-{self.port + self.port_range - 1}"

    def probe(self, port: int) -> Session | None:
        """Returns the session info of this app's bridge on `port`, or None."""
        try:
            with socket.create_connection((self.host, port), timeout=0.5) as sock:
                sock.settimeout(3)
                with sock.makefile("rwb") as fh:
                    resp = _exchange(fh, {"cmd": "ping"})
        except (OSError, ValueError):
            return None
        info = resp.get("result") if resp.get("ok") else None
        if not info:
            return None
        app = info.get("app", self.app if self.accept_missing_app else None)
        return info if app == self.app else None  # None: another app's bridge on this port

    def scan(self) -> list[Session]:
        """Finds every bridge of this app in the port range (probed in parallel)."""
        ports = range(self.port, self.port + self.port_range)
        with ThreadPoolExecutor(max_workers=len(ports) or 1) as pool:
            return [info for info in pool.map(self.probe, ports) if info]

    def describe(self, sessions: list[Session], selected_pid: int | None = None) -> str:
        lines = []
        for s in sessions:
            mark = "  <- selected" if s.get("pid") == selected_pid else ""
            busy = "  (busy)" if s.get("busy") else ""
            lines.append(f"- port {s['port']}: {self.session_label(s)}, pid {s['pid']}, "
                         f"{self.scene_key}: {s.get(self.scene_key)}{busy}{mark}")
        return "\n".join(lines)

    # -- routing ---------------------------------------------------------------------------
    def select(self, info: Session) -> None:
        with self._lock:
            if self.current is not None:
                self.current.close()
            self.current = _Client(self, info["port"], info["pid"])

    def _client(self) -> _Client:
        if self.current is not None:
            return self.current
        found = self.scan()
        if not found:
            raise BridgeError(f"No {self.label} found on {self.ports_text}. {self.not_found_hint}".rstrip())
        if len(found) > 1:
            raise BridgeError(
                f"{len(found)} {self.label} sessions are running and none is selected yet, so "
                f"nothing was run:\n{self.describe(found)}\n"
                f"Ask the user which one to use, then call {self.select_tool}(port=...)."
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

    # -- replies ---------------------------------------------------------------------------
    def format(self, resp: dict[str, Any]) -> str:
        """Turns a bridge reply into text for Claude; raises on app-side errors."""
        parts = []
        if resp.get("stdout"):
            parts.append("stdout:\n" + resp["stdout"].rstrip())
        if not resp.get("ok"):
            parts.append("error:\n" + (resp.get("error") or "unknown error").rstrip())
            raise BridgeError("\n\n".join(parts))
        result = resp.get("result")
        if result is not None:
            if isinstance(result, str) and "\n" in result:
                parts.append("result:\n" + result.rstrip())
            else:
                parts.append("result:\n" + json.dumps(result, indent=2, ensure_ascii=False))
        return "\n\n".join(parts) or "OK (no output)"

    def run(self, cmd: str, code: str, undo: bool = True, reset: bool | None = None) -> str:
        """Runs code in the app (cmd "python", "mel", "hscript" or "tcl")."""
        payload: dict[str, Any] = {"cmd": cmd, "code": code, "undo": undo}
        if reset is not None:
            payload["reset"] = reset
        return self.format(self.call(payload))

    def python(self, code: str) -> str:
        """Runs read-only helper code (not recorded in undo)."""
        return self.run("python", code, undo=False)

    def image(self, payload: dict[str, Any]) -> Image:
        """Sends a capture command and returns the PNG it answers with."""
        resp = self.call(payload)
        if not resp.get("ok"):
            self.format(resp)
        return Image(data=base64.b64decode(resp["result"]["base64"]), format="png")

    # -- the session tools every app has ---------------------------------------------------
    def status(self) -> str:
        resp = self.call({"cmd": "ping"})
        text = self.format(resp)
        bridge_version = (resp.get("result") or {}).get("bridge_version")
        if bridge_version != self.version:
            text += (f"\n\nNote: the bridge inside {self.label} is version {bridge_version} but "
                     f"this server is {self.version}. {self.update_hint}").rstrip()
        return text

    def list_sessions(self) -> str:
        found = self.scan()
        if not found:
            return f"No {self.label} found on {self.ports_text}."
        selected = self.current.pid if self.current else None
        note = "" if selected else "\n\nNo session selected yet."
        return self.describe(found, selected) + note

    def select_session(self, port: int) -> str:
        info = self.probe(port)
        if info is None:
            raise BridgeError(
                f"No {self.label} bridge answering on port {port}. Call {self.list_tool}.")
        self.select(info)
        return "Selected:\n" + self.describe([info], info["pid"])
