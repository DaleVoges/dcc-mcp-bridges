"""Tests dcc_mcp_core.Bridge against fake bridges, so no app needs to be installed.

    python core/tests/test_bridge.py

Fake bridges listen on ports 7690-7692 and answer the same JSON protocol as the real ones.
Covers: discovery and app filtering, auto-select with one session, refusal with two,
routing, error formatting, a closed session, and a different process taking over a port.
"""
import json
import os
import socket
import sys
import threading

os.environ["FAKE_MCP_PORT"] = "7690"
os.environ["FAKE_MCP_PORT_RANGE"] = "3"
os.environ["FAKE_MCP_TIMEOUT"] = "5"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dcc_mcp_core import Bridge, BridgeError, SessionGoneError  # noqa: E402


class FakeBridge:
    """A minimal in-app bridge: answers ping, and python by echoing the code back."""

    def __init__(self, port, pid, app="fake", scene="untitled"):
        self.port, self.pid, self.app, self.scene = port, pid, app, scene
        self.calls = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen()
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            self._answer(conn)
        except OSError:
            pass  # close() shut the connection down

    def _answer(self, conn):
        with conn, conn.makefile("rwb") as fh:
            for line in fh:
                req = json.loads(line)
                if req["cmd"] == "ping":
                    info = {"port": self.port, "pid": self.pid, "scene": self.scene,
                            "bridge_version": "1.0.0"}
                    if self.app is not None:
                        info["app"] = self.app
                    resp = {"ok": True, "result": info}
                elif req["code"] == "boom":
                    resp = {"ok": False, "error": "Traceback...\nZeroDivisionError", "stdout": "before\n"}
                else:
                    self.calls.append(req)
                    resp = {"ok": True, "result": req["code"]}
                resp["id"] = req["id"]
                fh.write((json.dumps(resp) + "\n").encode())
                fh.flush()

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass


def new_bridge():
    return Bridge("fake", "Fake", 7600, "1.0.0", lambda s: "Fake app",
                  not_found_hint="Open Fake.", update_hint="Update Fake.")


def expect_error(fn, cls=BridgeError, text=""):
    try:
        fn()
    except cls as exc:
        assert text in str(exc), str(exc)
        return str(exc)
    raise AssertionError("expected %s" % cls.__name__)


# Env vars come from the app name.
b = new_bridge()
assert (b.port, b.port_range, b.timeout) == (7690, 3, 5.0)
assert (b.list_tool, b.select_tool) == ("list_fake_sessions", "select_fake_session")

# No sessions: a clear error with the hint, and list says none.
assert "Open Fake." in expect_error(lambda: b.run("python", "1"), text="No Fake found on ports 7690-7692")
assert b.list_sessions() == "No Fake found on ports 7690-7692."
print("none found OK")

# Another app's bridge on the range is ignored.
other = FakeBridge(7692, 999, app="maya")
assert b.scan() == []
print("other app ignored OK")

# One session: auto-selected, calls routed to it, errors formatted.
a = FakeBridge(7690, 101, scene="a.hip")
assert b.run("python", "1+1") == 'result:\n"1+1"'
assert b.current.pid == 101
assert b.python("x") == 'result:\n"x"' and a.calls[-1]["undo"] is False
assert "reset" not in a.calls[-1]
b.run("python", "y", undo=True, reset=True)
assert a.calls[-1]["undo"] is True and a.calls[-1]["reset"] is True
err = expect_error(lambda: b.run("python", "boom"), text="ZeroDivisionError")
assert err.startswith("stdout:\nbefore\n\nerror:\n"), err
assert "version 1.0.0 but" not in b.status()
print("single session OK")

# Two sessions, none selected: refuse and list both.
b2 = new_bridge()
c = FakeBridge(7691, 202, scene="c.hip")
err = expect_error(lambda: b2.run("python", "1"), text="2 Fake sessions are running")
assert "port 7690: Fake app, pid 101, scene: a.hip" in err and "select_fake_session(port=...)" in err
assert "No session selected yet." in b2.list_sessions()
print("refusal OK")

# Selecting routes to the right one; list marks it.
b2.select_session(7691)
b2.run("python", "only in c")
assert c.calls[-1]["code"] == "only in c" and all(x["code"] != "only in c" for x in a.calls)
assert "pid 202, scene: c.hip  <- selected" in b2.list_sessions()
expect_error(lambda: b2.select_session(7692), text="No Fake bridge answering on port 7692")
print("routing OK")

# The selected session closes: nothing runs, and the selection is cleared.
c.close()
expect_error(lambda: b2.run("python", "1"), SessionGoneError)
assert b2.current is None
print("closed session OK")

# A different process takes over the selected port: refuse instead of running there.
b2.select_session(7690)
b2.run("python", "open connection")
a.close()
imposter = FakeBridge(7690, 303)
err = expect_error(lambda: b2.run("python", "must not run"), SessionGoneError, text="pid 101) is gone")
assert not imposter.calls and b2.current is None
print("port takeover OK")

# Plug-ins whose ping has no "app" field (the Maya plug-in) count only when allowed.
imposter.app = None
assert new_bridge().probe(7690) is None
legacy = Bridge("fake", "Fake", 7600, "1.0.0", lambda s: "Fake", accept_missing_app=True)
assert legacy.probe(7690)["pid"] == 303
print("missing app field OK")
print("ALL OK")
