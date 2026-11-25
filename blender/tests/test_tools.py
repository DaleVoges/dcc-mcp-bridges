"""Calls each tool directly against a test bridge (tests/run_bridge_blender.py).

Builds a few objects in the test Blender, so never point it at a real Blender.
"""
import os
import sys

# Tests only use ports 7690-7691, so they can never reach a real Blender on 7677+.
os.environ["BLENDER_MCP_PORT"] = "7690"
os.environ["BLENDER_MCP_PORT_RANGE"] = "2"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "core")]
from blender_mcp_bridge import server as s  # noqa: E402

PNG = bytes([0x89]) + b"PNG"


def show(title, text):
    print(f"--- {title}\n{text[:1500]}\n")


show("status", s.blender_status())
show("sessions", s.list_blender_sessions())

out = s.execute_python('''
coll = D.collections.new("claude_test")
C.scene.collection.children.link(coll)
me = D.meshes.new("claude_box")
me.from_pydata([(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], [],
               [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
box = D.objects.new("claude_box", me)
coll.objects.link(box)
box.location = (1, 2, 3)
sub = box.modifiers.new("Subd", "SUBSURF")
sub.levels = 2
mat = D.materials.new("claude_mat")
mat.use_nodes = True
mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.123
me.materials.append(mat)
print("built", box.name)
box
''')
show("build", out)
assert "claude_box" in out, out

out = s.execute_python("coll.objects[:]")  # namespace persists
assert "claude_box" in out, out
out = s.execute_python("Vector((1, 2, 3))")
assert "1.0" in out and "3.0" in out, out
out = s.execute_python("D.objects['claude_box'].matrix_world")
assert "[" in out, out

show("scene info", s.get_scene_info())
out = s.list_objects(collection="claude_test")
assert "claude_box" in out, out
out = s.list_objects(pattern="claude_*", object_type="mesh")
assert "claude_box" in out, out

out = s.inspect_object("claude_box")
show("inspect", out)
assert '"vertices": 8' in out and "SUBSURF" in out and '"levels": 2' in out, out
assert "evaluated_mesh" in out and "claude_mat" in out, out

out = s.inspect_node_tree("claude_mat")
show("node tree", out)
assert "Principled BSDF" in out and "0.123" in out and "Material Output" in out, out
assert '"IOR"' not in out, "unchanged inputs should be left out"
out = s.execute_python("[g.name for g in D.node_groups]", undoable=False)
assert "_claude_defaults" not in out, out

out = s.get_bpy_help("SubsurfModifier")
assert '"levels"' in out, out[:500]
out = s.get_bpy_help("ops.mesh.primitive_cube_add")
assert '"size"' in out, out[:500]
out = s.get_bpy_help("subsurf")
assert "SubsurfModifier" in out, out[:500]
out = s.get_bpy_help("utils.register_class")
assert "register" in out, out[:500]

try:
    s.execute_python("1/0")
    raise AssertionError("should fail")
except s.BridgeError as exc:
    assert "ZeroDivisionError" in str(exc)
    print("error OK")

img = s.capture_viewport(320, 180)  # no UI: renders through the scene camera
assert img.data[:4] == PNG, img.data[:8]
print("capture through scene camera OK:", len(img.data), "bytes")

s.execute_python("_saved_cam = C.scene.camera; C.scene.camera = None", undoable=False)
try:
    s.capture_viewport()
    raise AssertionError("capture should fail without UI or camera")
except s.BridgeError as exc:
    print("capture without UI or camera OK ->", str(exc).splitlines()[-1])
s.execute_python("C.scene.camera = _saved_cam", undoable=False)

s.execute_python('''
cam = D.objects.new("claude_cam", D.cameras.new("claude_cam"))
coll.objects.link(cam)
cam.location = (8, -8, 6)
cam.rotation_euler = (1.1, 0, 0.785)
''')
img = s.capture_viewport(320, 180, "claude_cam")
assert img.data[:4] == PNG, img.data[:8]
out = s.execute_python("C.scene.camera", undoable=False)
assert "claude_cam" not in out, "scene camera should be restored: " + out
print("capture through claude_cam OK:", len(img.data), "bytes")

s.execute_python('''
for o in list(coll.objects):
    D.objects.remove(o)
D.collections.remove(coll)
D.materials.remove(D.materials["claude_mat"])
D.meshes.remove(D.meshes["claude_box"])
D.cameras.remove(D.cameras["claude_cam"])
''', undoable=False)
print("TOOLS TEST OK")
