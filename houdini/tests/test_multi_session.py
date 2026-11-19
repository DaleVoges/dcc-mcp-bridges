"""Several Houdinis: refusal, routing, a closed Houdini, and port takeover.

    python tests/test_multi_session.py "C:/Program Files/Side Effects Software/Houdini 21.0.700/bin/hython.exe"

Starts its own hython bridges on ports 7490-7491 and kills them afterwards.
"""
import os
import subprocess
import sys

# Tests only use ports 7490-7491, so they can never reach a real Houdini on 7477+.
os.environ["HOUDINI_MCP_PORT"] = "7490"
os.environ["HOUDINI_MCP_PORT_RANGE"] = "2"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "core")]
from houdini_mcp_bridge import server as s  # noqa: E402

HYTHON = sys.argv[1]
procs = []


def launch(port):
    env = dict(os.environ, HOUDINI_MCP_PORT=str(port), HOUDINI_MCP_PORT_RANGE="1")
    p = subprocess.Popen([HYTHON, os.path.join(HERE, "run_bridge_hython.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        if "READY" in line:
            break
    procs.append(p)
    return p


try:
    a, b = launch(7490), launch(7491)
    found = s.bridge.scan()
    assert sorted(i["pid"] for i in found) == sorted([a.pid, b.pid]), found
    print("scan OK: two sessions")

    try:
        s.execute_python("hou.node('/obj').createNode('null', 'mustNotExist')")
        raise AssertionError("ran with two sessions and none selected")
    except s.BridgeError as exc:
        assert "none is selected" in str(exc)
        print("refused OK with two sessions")

    s.select_houdini_session(7491)
    assert str(b.pid) in s.execute_python("import os; os.getpid()")
    s.select_houdini_session(7490)
    assert str(a.pid) in s.execute_python("import os; os.getpid()")
    print("routing OK")

    # The selected Houdini dies and another one takes its port: nothing may run there.
    a.kill()
    a.wait()
    c = launch(7490)
    try:
        s.execute_python("hou.node('/obj').createNode('null', 'mustNotExist')")
        raise AssertionError("ran on a different Houdini!")
    except s.SessionGoneError as exc:
        assert str(c.pid) in str(exc)
        print("takeover refused OK ->", str(exc).splitlines()[0][:110])
    assert s.bridge.current is None, "dead session still selected"

    s.select_houdini_session(7490)
    assert "true" in s.execute_python("hou.node('/obj/mustNotExist') is None").lower()
    print("MULTI SESSION TEST OK")
finally:
    for p in procs:
        p.kill()
