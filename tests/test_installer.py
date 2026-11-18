"""Tests drag_to_install.py in headless Maya against a throwaway prefs folder.

    set MAYA_APP_DIR=<empty temp folder>
    mayapy tests/test_installer.py <unzipped release folder> install|check-module

`install` runs install/update/uninstall; `check-module` (run in a fresh mayapy afterwards)
checks that the .mod file alone lets Maya find the plug-in by name.
"""
from __future__ import print_function

import json
import os
import socket
import sys

TEST_PORT = 7390  # away from 7377+ so tests never reach a real Maya
os.environ["MAYA_MCP_PORT"] = str(TEST_PORT)
os.environ["MAYA_MCP_PORT_RANGE"] = "1"

import maya.standalone

maya.standalone.initialize(name="python")
import maya.cmds as cmds  # noqa: E402

release_dir, mode = sys.argv[1], sys.argv[2]
sys.path.insert(0, release_dir)
import drag_to_install as inst  # noqa: E402

modules_dir = inst._modules_dir()
assert inst._long_path(os.environ["MAYA_APP_DIR"]).replace("\\", "/").rstrip("/").lower() in modules_dir.lower(), \
    "refusing to run against real Maya prefs: %s" % modules_dir


def ping():
    sock = socket.create_connection(("127.0.0.1", TEST_PORT), timeout=5)
    fh = sock.makefile("rwb")
    fh.write(b'{"cmd": "ping"}\n')
    fh.flush()
    info = json.loads(fh.readline().decode("utf-8"))["result"]
    sock.close()
    return info


if mode == "install":
    if not os.path.isdir(modules_dir):
        os.makedirs(modules_dir)
    legacy = os.path.join(modules_dir, "claude_mcp.mod")
    with open(legacy, "w") as fh:
        fh.write("+ ClaudeMCP 1.0.0 C:/old/dev/path\n")

    assert inst.installed_version() is None
    v = inst.install()
    assert inst.installed_version() == v, inst.installed_version()
    assert not os.path.exists(legacy), "legacy .mod not removed"
    assert cmds.pluginInfo("claude_mcp_bridge", query=True, loaded=True)
    info = ping()
    assert info["bridge_version"] == v
    print("install OK: version %s, bridge answering on port %s" % (v, info["port"]))

    inst.install()  # update over an existing, loaded install
    assert cmds.pluginInfo("claude_mcp_bridge", query=True, loaded=True)
    ping()
    print("update OK")

    inst.uninstall()
    assert inst.installed_version() is None
    assert not os.path.isdir(os.path.join(modules_dir, "maya_mcp_bridge"))
    assert not cmds.pluginInfo("claude_mcp_bridge", query=True, loaded=True)
    print("uninstall OK")

    inst.install(load=False)  # leave it installed for the check-module run
    print("INSTALL TESTS OK")

elif mode == "check-module":
    # Fresh Maya: only the .mod file tells it where the plug-in is.
    cmds.loadPlugin("claude_mcp_bridge.py")  # Python plug-ins load by file name
    path = cmds.pluginInfo("claude_mcp_bridge", query=True, path=True)
    assert "maya_mcp_bridge" in path, path
    print("module OK: Maya found the plug-in at %s" % path)
    print("bridge answering:", ping()["maya_version"])
    print("MODULE TEST OK")
