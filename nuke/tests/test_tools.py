"""Calls each tool directly against a test bridge (tests/run_bridge_nuke.py).

    python tests/test_tools.py [folder to save the rendered frame in]

Builds a small node tree in the test Nuke, so never point it at a real Nuke.
"""
import os
import sys

# Tests only use ports 7590-7591, so they can never reach a real Nuke on 7577+.
os.environ["NUKE_MCP_PORT"] = "7590"
os.environ["NUKE_MCP_PORT_RANGE"] = "2"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from nuke_mcp_bridge import server as s  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else None


def show(title, text):
    print(f"--- {title}\n{text[:1200]}\n")


show("status", s.nuke_status())

out = s.execute_python('''
for n in nuke.allNodes():
    nuke.delete(n)
checker = nuke.nodes.CheckerBoard2(name="Checker")
grade = nuke.nodes.Grade(name="Grade_test", inputs=[checker])
grade["white"].setValue([1.5, 0.6, 0.3, 1.0])
blur = nuke.nodes.Blur(name="Blur_test", inputs=[grade])
blur["size"].setExpression("frame / 2")
print("built", blur.name())
blur
''')
show("build tree", out)
assert "Blur_test" in out, out

assert "Grade_test" in s.execute_python("blur.input(0)")  # namespace persists
out = s.execute_python("nuke.root().format()")
assert '"width"' in out, out

show("tcl", s.execute_tcl("value root.first_frame"))

show("script info", s.get_script_info())
out = s.list_nodes(node_class="Grade")
assert "Grade_test" in out and "Blur_test" not in out, out

out = s.inspect_node("Grade_test")
show("inspect grade", out)
assert '"white"' in out and "Checker" in out and '"bbox"' in out and '"rgba.red"' in out, out
out = s.inspect_node("Blur_test")
assert "frame / 2" in out or "frame/2" in out, out

out = s.get_node_class_info("Grade")
assert '"blackpoint"' in out and '"mix"' in out, out[:500]
assert "Grade" not in s.execute_python("[n.Class() for n in nuke.allNodes() if n.name().startswith('Grade') and n.name() != 'Grade_test']"), \
    "temporary node left behind"
print("class info OK (temporary node removed)")

show("nuke help", s.get_nuke_help("Node.setInput")[:300])

img = s.render_frame("Blur_test", frame=10, width=480)
print("render OK: %d bytes" % len(img.data))
assert img.data[:4] == b"\x89PNG"
if OUT:
    with open(os.path.join(OUT, "nuke_render.png"), "wb") as fh:
        fh.write(img.data)
assert "Reformat" not in s.execute_python("[n.Class() for n in nuke.allNodes()]"), "temp nodes left"

s.execute_python('nuke.nodes.Read(name="Missing", file="C:/does/not/exist.####.exr", first=1, last=10)')
out = s.find_node_errors()
show("errors", out)
assert "Missing" in out, out

# (Nuke disables undo in terminal mode; tests/test_gui.py checks undo.)

try:
    s.execute_python("1/0")
    raise AssertionError("should fail")
except s.NukeBridgeError as exc:
    assert "ZeroDivisionError" in str(exc)
    print("error OK")

s.execute_python("[nuke.delete(n) for n in nuke.allNodes()]", undoable=False)
print("TOOLS TEST OK")
