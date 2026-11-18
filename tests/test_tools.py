"""Calls the MCP server's tools against a running bridge (Maya GUI or run_bridge_mayapy.py)."""
import os
import sys

# Tests only use ports 7390-7391, so they can never reach (or wipe) a real Maya on 7377+.
os.environ["MAYA_MCP_PORT"] = "7390"
os.environ["MAYA_MCP_PORT_RANGE"] = "2"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from maya_mcp_bridge import server as s  # noqa: E402

print(s.maya_status())
print(s.execute_python("cmds.file(new=True, force=True)\ncube = cmds.polyCube(name='claudeCube')[0]\n"
                       "cmds.move(1, 2, 3, cube)\ncmds.xform(cube, q=True, t=True, ws=True)"))
print(s.execute_python("print('persisted:', cube)\nresult = om.MVector(1, 2, 3)"))
try:
    print(s.execute_python("pm.PyNode(cube).getTranslation()"))
except s.MayaBridgeError as exc:
    print("pymel unavailable ->", str(exc).splitlines()[-1])
print(s.execute_mel("polySphere -n melSphere;"))
print(s.list_nodes(node_type="mesh"))
print(s.inspect_node("claudeCube")[:600])
print(s.get_scene_info()[:600])
print(s.get_command_help("polyCube")[:300])
try:
    s.execute_python("cmds.nonexistentCommand()")
except s.MayaBridgeError as exc:
    print("expected error ->", str(exc).splitlines()[-1])
print("ALL OK")
