"""Graphical Houdini: the package autostarts the bridge, and capture_viewport works.

    python tests/test_gui.py "<Houdini>/bin/houdinicore.exe" <folder to save captures in>

Installs into a throwaway prefs folder, opens Houdini with it (a window appears for about a
minute), runs the tools on port 7490, then closes that Houdini.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

os.environ["HOUDINI_MCP_PORT"] = "7490"
os.environ["HOUDINI_MCP_PORT_RANGE"] = "2"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)
from houdini_mcp_bridge import server as s  # noqa: E402

HOUDINI, OUT = sys.argv[1], sys.argv[2]
version = re.search(r"Houdini (\d+\.\d+)", HOUDINI).group(1)
root = tempfile.mkdtemp(prefix="hmcp_gui_")
os.makedirs(os.path.join(root, "houdini" + version))
env = dict(os.environ, PYTHONPATH=SRC, HOUDINI_USER_PREF_DIR=os.path.join(root, "houdini__HVER__"))
subprocess.run([sys.executable, "-m", "houdini_mcp_bridge", "install"], env=env, check=True,
               capture_output=True)

proc = subprocess.Popen([HOUDINI], env=env)
try:
    info = None
    for _ in range(240):
        found = [i for i in s.scan() if i["pid"] == proc.pid]
        if found:
            info = found[0]
            break
        time.sleep(1)
    assert info, "graphical Houdini did not start the bridge"
    assert info["ui"] is True, info
    print("autostart OK:", s._describe([info]))
    s.select_houdini_session(info["port"])

    print(s.execute_python('''
geo = hou.node("/obj").createNode("geo", "gui_test")
t = geo.createNode("testgeometry_rubbertoy") if "testgeometry_rubbertoy" in hou.sopNodeTypeCategory().nodeTypes() else geo.createNode("sphere")
t.setDisplayFlag(True); t.setRenderFlag(True)
viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
viewer.curViewport().frameAll()
t.path()
'''))

    img = s.capture_viewport(800, 450)
    with open(os.path.join(OUT, "houdini_capture.png"), "wb") as fh:
        fh.write(img.data)
    print("capture OK: %d bytes" % len(img.data))

    s.execute_python('''
cam = hou.node("/obj").createNode("cam", "side_cam")
cam.parmTuple("t").set((8, 1, 0)); cam.parmTuple("r").set((0, 90, 0))
''')
    img = s.capture_viewport(640, 360, camera="/obj/side_cam")
    with open(os.path.join(OUT, "houdini_capture_cam.png"), "wb") as fh:
        fh.write(img.data)
    after = s.execute_python("hou.ui.paneTabOfType(hou.paneTabType.SceneViewer).curViewport().camera()")
    assert "side_cam" not in after, "viewport camera not restored: " + after
    print("camera capture OK, viewport restored")

    out = s.execute_python("hou.isUIAvailable()")
    assert "true" in out
    print("GUI TEST OK")
finally:
    proc.kill()
    time.sleep(2)
    shutil.rmtree(root, ignore_errors=True)
