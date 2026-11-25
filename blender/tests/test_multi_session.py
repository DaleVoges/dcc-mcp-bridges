"""Several Blenders: refusal, routing, a closed Blender, and port takeover.

    python tests/test_multi_session.py "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"

Starts its own blender bridges on ports 7690-7691 and kills them afterwards.
"""
import os
import subprocess
import sys

# Tests only use ports 7690-7691, so they can never reach a real Blender on 7677+.
os.environ["BLENDER_MCP_PORT"] = "7690"
os.environ["BLENDER_MCP_PORT_RANGE"] = "2"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "core")]
from blender_mcp_bridge import server as s  # noqa: E402

BLENDER = sys.argv[1]
procs = []


def launch(port):
    env = dict(os.environ, BLENDER_MCP_PORT=str(port), BLENDER_MCP_PORT_RANGE="1")
    p = subprocess.Popen([BLENDER, "--background", "--factory-startup", "--python",
                          os.path.join(HERE, "run_bridge_blender.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        if "READY" in line:
            break
    procs.append(p)
    return p


try:
    a, b = launch(7690), launch(7691)
    found = s.bridge.scan()
    assert sorted(i["pid"] for i in found) == sorted([a.pid, b.pid]), found
    print("scan OK: two sessions")

    try:
        s.execute_python("C.scene.collection.objects.link(D.objects.new('mustNotExist', None))")
        raise AssertionError("ran with two sessions and none selected")
    except s.BridgeError as exc:
        assert "none is selected" in str(exc)
        print("refused OK with two sessions")

    s.select_blender_session(7691)
    assert str(b.pid) in s.execute_python("import os; os.getpid()")
    s.select_blender_session(7690)
    assert str(a.pid) in s.execute_python("import os; os.getpid()")
    print("routing OK")

    # The selected Blender dies and another one takes its port: nothing may run there.
    a.kill()
    a.wait()
    c = launch(7690)
    try:
        s.execute_python("C.scene.collection.objects.link(D.objects.new('mustNotExist', None))")
        raise AssertionError("ran on a different Blender!")
    except s.SessionGoneError as exc:
        assert str(c.pid) in str(exc)
        print("takeover refused OK ->", str(exc).splitlines()[0][:110])
    assert s.bridge.current is None, "dead session still selected"

    s.select_blender_session(7690)
    assert "true" in s.execute_python("'mustNotExist' not in D.objects").lower()
    print("MULTI SESSION TEST OK")
finally:
    for p in procs:
        p.kill()
