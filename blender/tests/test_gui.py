"""Tests the bridge in graphical Blender: autostart, the main-thread timer, undo, viewport capture.

    python tests/test_gui.py "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"

Installs the startup script into a throwaway config folder, opens a Blender window that uses
it (BLENDER_USER_RESOURCES) on test port 7690, runs the checks, then closes that Blender.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Tests only use ports 7690-7691, so they can never reach a real Blender on 7677+.
os.environ["BLENDER_MCP_PORT"] = "7690"
os.environ["BLENDER_MCP_PORT_RANGE"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "core")]
from blender_mcp_bridge import install, server as s  # noqa: E402

BLENDER = sys.argv[1]
PNG = bytes([0x89]) + b"PNG"

root = tempfile.mkdtemp(prefix="bmcp_gui_")
proc = None
try:
    install.install_into(__import__("pathlib").Path(root))
    env = dict(os.environ, BLENDER_USER_RESOURCES=root)
    proc = subprocess.Popen([BLENDER], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(120):
        found = [i for i in s.bridge.scan() if i["pid"] == proc.pid]
        if found:
            break
        time.sleep(1)
    else:
        raise AssertionError("graphical Blender did not start the bridge")
    assert found[0]["ui"] is True, found
    print("autostart OK:", s.list_blender_sessions().splitlines()[0])

    s.execute_python("o = D.objects.new('keep_me', None); C.scene.collection.objects.link(o)")
    out = s.execute_python("o = D.objects.new('undo_me', None); C.scene.collection.objects.link(o)\n"
                           "o.location.x = 5\no")
    assert "undo_me" in out, out
    s.execute_python("with bpy.context.temp_override(**view3d_override()):\n"
                     "    bpy.ops.ed.undo()", undoable=False)
    out = s.execute_python("'undo_me' in D.objects, 'keep_me' in D.objects", undoable=False)
    assert "".join(out.lower().split()).endswith("[false,true]"),"one Ctrl+Z should undo only the last call: " + out
    print("undo OK: one call = one undo step")

    img = s.capture_viewport(400, 300)
    assert img.data[:4] == PNG, img.data[:8]
    img = s.capture_viewport(400, 300, "Camera")
    assert img.data[:4] == PNG, img.data[:8]
    print("viewport capture OK (3D View and through Camera)")

    out = s.execute_python("C.scene.render.resolution_x, C.scene.camera", undoable=False)
    assert "1920" in out and "Camera" in out, "render settings should be restored: " + out
    print("GUI TEST OK")
finally:
    if proc is not None:
        proc.kill()
        proc.wait()
    shutil.rmtree(root, ignore_errors=True)
