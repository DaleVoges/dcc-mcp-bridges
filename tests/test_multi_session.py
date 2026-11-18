"""Multi-Maya test. Start two bridges first (run tests\\run_bridge_mayapy.py twice).

Checks: each Maya got its own port; tools refuse to guess with two running; selecting
routes calls to the right Maya; ping answers while Maya is busy; a vanished Maya is
reported instead of silently switching.
"""
import os
import sys

# Tests only use ports 7390-7391, so they can never reach (or wipe) a real Maya on 7377+.
os.environ["MAYA_MCP_PORT"] = "7390"
os.environ["MAYA_MCP_PORT_RANGE"] = "2"
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from maya_mcp_bridge import server as s  # noqa: E402

found = s.scan()
print(s.list_maya_sessions())
assert len(found) == 2, "expected 2 Mayas, found %d" % len(found)
assert found[0]["port"] != found[1]["port"] and found[0]["pid"] != found[1]["pid"]

# 1. With two running and none selected, tools must refuse.
try:
    s.execute_python("cmds.polyCube()")
    raise AssertionError("ran without a selected session")
except s.MayaBridgeError as exc:
    print("refused OK ->", str(exc).splitlines()[0])

# 2. Select each one in turn and tag its scene; confirm they stay separate.
a, b = found
s.select_maya_session(a["port"])
s.execute_python("cmds.file(new=True, force=True); cmds.polyCube(name='onlyInA')")
s.select_maya_session(b["port"])
s.execute_python("cmds.file(new=True, force=True); cmds.polySphere(name='onlyInB')")
assert "false" in s.execute_python("cmds.objExists('onlyInA')").lower()
s.select_maya_session(a["port"])
assert "true" in s.execute_python("cmds.objExists('onlyInA')").lower()
assert "false" in s.execute_python("cmds.objExists('onlyInB')").lower()
print("routing OK")

# 3. Ping while Maya A is busy on its main thread.
t = threading.Thread(target=s.execute_python, args=("import time; time.sleep(3)",))
t.start()
time.sleep(0.5)
start = time.time()
busy_info = s.probe(a["port"])
print("ping while busy took %.2fs, busy=%s" % (time.time() - start, busy_info["busy"]))
assert busy_info["busy"] is True and time.time() - start < 1
t.join()

# 4. Selected Maya disappears.
s.select_maya_session(a["port"])
os.kill(a["pid"], 9)
time.sleep(1)
try:
    s.execute_python("1")
    raise AssertionError("ran on a dead session")
except s.MayaBridgeError as exc:
    print("gone OK ->", str(exc).splitlines()[0])
assert s.maya.current is None
# Only B is left now, so the next call auto-selects it.
print(s.maya_status())
print("ALL MULTI-SESSION TESTS OK")
