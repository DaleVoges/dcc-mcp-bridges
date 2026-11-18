"""Calls each tool directly against a test bridge (tests/run_bridge_hython.py).

Builds a small SOP network in the test hython, so never point it at a real Houdini.
"""
import os
import sys

# Tests only use ports 7490-7491, so they can never reach a real Houdini on 7477+.
os.environ["HOUDINI_MCP_PORT"] = "7490"
os.environ["HOUDINI_MCP_PORT_RANGE"] = "2"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from houdini_mcp_bridge import server as s  # noqa: E402


def show(title, text):
    print(f"--- {title}\n{text[:1500]}\n")


show("status", s.houdini_status())
show("sessions", s.list_houdini_sessions())

out = s.execute_python('''
geo = hou.node("/obj").createNode("geo", "claude_test")
box = geo.createNode("box")
box.parm("sizex").set(2)
mnt = geo.createNode("mountain::2.0") if "mountain::2.0" in hou.sopNodeTypeCategory().nodeTypes() else geo.createNode("mountain")
mnt.setFirstInput(box)
mnt.setDisplayFlag(True); mnt.setRenderFlag(True)
geo.layoutChildren()
print("built", mnt.path())
mnt
''')
show("build network", out)
assert "/obj/claude_test/" in out

out = s.execute_python("geo.children()")  # namespace persists
assert "box1" in out, out

out = s.execute_python("hou.Vector3(1, 2, 3)")
assert "1.0" in out and "3.0" in out, out
out = s.execute_python("hou.node('/obj/claude_test').worldTransform()")
assert "[" in out, out

show("hscript", s.execute_hscript("opls /obj"))
try:
    s.execute_hscript("thisisnotacommand")
    raise AssertionError("bad hscript should fail")
except s.HoudiniBridgeError as exc:
    print("hscript error OK ->", str(exc).splitlines()[-1])

show("scene info", s.get_scene_info())
out = s.list_nodes("/obj/claude_test")
assert '"box"' in out, out
out = s.list_nodes("/obj", node_type="box", recursive=True)
assert "box1" in out, out

out = s.inspect_node("/obj/claude_test/box1")
show("inspect box", out)
assert '"sizex"' in out and '"points": 8' in out, out

out = s.get_node_type_info("box", "Sop")
assert '"sizex"' in out or '"size"' in out, out[:500]
out = s.get_node_type_info("*wrangle*", "Sop")
show("type search", out)
assert "attribwrangle" in out, out

show("hou help", s.get_hou_help("Node.createNode")[:400])

# A broken node shows up in find_node_errors.
s.execute_python('''
w = hou.node("/obj/claude_test").createNode("attribwrangle", "broken")
w.setFirstInput(hou.node("/obj/claude_test/box1"))
w.parm("snippet").set("this is not vex;")
try:
    w.cook(force=True)
except hou.OperationFailed:
    pass
''')
out = s.find_node_errors("/obj/claude_test")
show("errors", out)
assert "broken" in out, out

# Undo: one call = one undo group.
s.execute_python("hou.node('/obj').createNode('null', 'undo_me')")
s.execute_python("hou.undos.performUndo()", undoable=False)
out = s.execute_python("hou.node('/obj/undo_me') is None", undoable=False)
print("undo removed node:", out.strip().splitlines()[-1])

try:
    s.execute_python("1/0")
    raise AssertionError("should fail")
except s.HoudiniBridgeError as exc:
    assert "ZeroDivisionError" in str(exc)
    print("error OK")

try:
    s.capture_viewport()
    raise AssertionError("capture should fail without UI")
except s.HoudiniBridgeError as exc:
    print("capture without UI OK ->", str(exc).splitlines()[-1])

s.execute_python("hou.node('/obj/claude_test').destroy()", undoable=False)
print("TOOLS TEST OK")
