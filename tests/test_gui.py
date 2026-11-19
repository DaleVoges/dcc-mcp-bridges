"""Graphical Nuke: menu.py autostarts the bridge; undo and render_frame work in the GUI.

    python tests/test_gui.py "<Nuke>/Nuke16.0.exe" <folder to save the render in>

Installs into a throwaway HOME/.nuke, opens Nuke with it (a window appears for about a
minute), runs the tools on port 7590, then closes that Nuke.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ["NUKE_MCP_PORT"] = "7590"
os.environ["NUKE_MCP_PORT_RANGE"] = "2"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)
from nuke_mcp_bridge import server as s  # noqa: E402

NUKE, OUT = sys.argv[1], sys.argv[2]
home = tempfile.mkdtemp(prefix="nmcp_gui_")
env = dict(os.environ, PYTHONPATH=SRC, HOME=home)
subprocess.run([sys.executable, "-m", "nuke_mcp_bridge", "install"], env=env, check=True,
               capture_output=True)

proc = subprocess.Popen([NUKE], env=env)
try:
    info = None
    for _ in range(240):
        found = [i for i in s.scan() if i["pid"] == proc.pid]
        if found:
            info = found[0]
            break
        time.sleep(1)
    assert info, "graphical Nuke did not start the bridge"
    assert info["gui"] is True and info["script"] == "Untitled", info
    print("autostart OK:", s._describe([info]))
    s.select_nuke_session(info["port"])

    out = s.execute_python('[i.name() for i in nuke.menu("Nuke").findItem("Edit/Claude MCP").items()]')
    assert "Start Bridge" in out, out
    print("menu OK")

    s.execute_python("nuke.nodes.NoOp(name='undo_me')")
    s.execute_python("nuke.undo()", undoable=False)
    assert "true" in s.execute_python("nuke.toNode('undo_me') is None", undoable=False).lower()
    print("undo OK: one call = one undo step")

    s.execute_python('''
r = nuke.nodes.Radial(name="Radial_test")
g = nuke.nodes.Grade(name="Grade_gui", inputs=[r])
g["white"].setValue([0.2, 0.6, 1.0, 1.0])
v = nuke.toNode("Viewer1") or nuke.nodes.Viewer()
v.setInput(0, g)
''')
    img = s.render_frame(width=640)  # no node given: uses what the Viewer shows
    with open(os.path.join(OUT, "nuke_gui_render.png"), "wb") as fh:
        fh.write(img.data)
    print("render from Viewer OK: %d bytes" % len(img.data))
    print("GUI TEST OK")
finally:
    proc.kill()
    time.sleep(2)
    shutil.rmtree(home, ignore_errors=True)
