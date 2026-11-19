"""Several Nukes: refusal, routing, a closed Nuke, and port takeover.

    python tests/test_multi_session.py "C:/Program Files/Nuke16.0v8/Nuke16.0.exe"

Starts its own nuke bridges on ports 7590-7591 and kills them afterwards.
"""
import os
import subprocess
import sys

# Tests only use ports 7590-7591, so they can never reach a real Nuke on 7577+.
os.environ["NUKE_MCP_PORT"] = "7590"
os.environ["NUKE_MCP_PORT_RANGE"] = "2"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "core")]
from nuke_mcp_bridge import server as s  # noqa: E402

NUKE = sys.argv[1]
procs = []


def launch(port):
    env = dict(os.environ, NUKE_MCP_PORT=str(port), NUKE_MCP_PORT_RANGE="1")
    p = subprocess.Popen([NUKE, "-t", os.path.join(HERE, "run_bridge_nuke.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        if "READY" in line:
            break
    procs.append(p)
    return p


try:
    a, b = launch(7590), launch(7591)
    found = s.bridge.scan()
    assert sorted(i["pid"] for i in found) == sorted([a.pid, b.pid]), found
    print("scan OK: two sessions")

    try:
        s.execute_python("nuke.nodes.NoOp(name='mustNotExist')")
        raise AssertionError("ran with two sessions and none selected")
    except s.BridgeError as exc:
        assert "none is selected" in str(exc)
        print("refused OK with two sessions")

    s.select_nuke_session(7591)
    assert str(b.pid) in s.execute_python("import os; os.getpid()")
    s.select_nuke_session(7590)
    assert str(a.pid) in s.execute_python("import os; os.getpid()")
    print("routing OK")

    # The selected Nuke dies and another one takes its port: nothing may run there.
    a.kill()
    a.wait()
    c = launch(7590)
    try:
        s.execute_python("nuke.nodes.NoOp(name='mustNotExist')")
        raise AssertionError("ran on a different Nuke!")
    except s.SessionGoneError as exc:
        assert str(c.pid) in str(exc)
        print("takeover refused OK ->", str(exc).splitlines()[0][:110])
    assert s.bridge.current is None, "dead session still selected"

    s.select_nuke_session(7590)
    assert "true" in s.execute_python("nuke.toNode('mustNotExist') is None").lower()
    print("MULTI SESSION TEST OK")
finally:
    for p in procs:
        p.kill()
