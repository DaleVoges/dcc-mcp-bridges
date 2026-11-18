"""The selected Maya dies and a different Maya takes over its port: nothing may run there.

Needs one bridge running already (tests\\run_bridge_mayapy.py). Pass the mayapy path as argv[1].
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import maya_mcp_server as s  # noqa: E402

mayapy = sys.argv[1]
old = s.scan()[0]
s.select_maya_session(old["port"])
s.execute_python("1")  # open the connection
os.kill(old["pid"], 9)

env = dict(os.environ, MAYA_MCP_PORT=str(old["port"]), MAYA_MCP_PORT_RANGE="1")
new = subprocess.Popen([mayapy, os.path.join(HERE, "run_bridge_mayapy.py")], env=env,
                       stdout=subprocess.PIPE, text=True)
try:
    for line in new.stdout:
        if "READY" in line:
            break
    try:
        s.execute_python("cmds.polyCube(name='mustNotExist')")
        raise AssertionError("ran on a different Maya!")
    except s.MayaBridgeError as exc:
        print("refused OK ->", str(exc).splitlines()[0])
    info = s.probe(old["port"])
    assert info["pid"] == new.pid or info["pid"] != old["pid"]
    s.select_maya_session(old["port"])
    assert "false" in s.execute_python("cmds.objExists('mustNotExist')").lower()
    print("TAKEOVER TEST OK")
finally:
    new.kill()
