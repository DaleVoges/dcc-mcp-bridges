"""Tests `houdini-mcp-bridge install/uninstall` against throwaway prefs folders.

    python tests/test_installer.py <hython.exe> [<another hython.exe> ...]

Creates a temp folder with one houdiniX.Y prefs folder per hython given, points
HOUDINI_USER_PREF_DIR at it, installs, then checks in each hython that Houdini loaded the
package (pythonrc.py ran and imported the bridge) without starting it (no UI).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.pathsep.join([os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "core")])
HYTHONS = sys.argv[1:]

CHECK = r'''
import sys, json, hou
mod = sys.modules.get("claude_mcp_bridge")
print("CHECK " + json.dumps({
    "version": hou.applicationVersionString(),
    "pythonrc_imported_bridge": mod is not None,
    "bridge_file": getattr(mod, "__file__", None),
    "running": bool(mod and mod.is_running()),
}))
'''


def cli(*args, env):
    out = subprocess.run([sys.executable, "-m", "houdini_mcp_bridge", *args], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


root = tempfile.mkdtemp(prefix="hmcp_install_")
try:
    versions = []
    for hython in HYTHONS:
        v = re.search(r"Houdini (\d+\.\d+)", hython).group(1)
        versions.append(v)
        os.makedirs(os.path.join(root, "houdini" + v))
    env = dict(os.environ, PYTHONPATH=SRC,
               HOUDINI_USER_PREF_DIR=os.path.join(root, "houdini__HVER__"))

    out = cli("install", env=env)
    print(out.strip().splitlines()[0])
    for v in versions:
        pkg = os.path.join(root, "houdini" + v, "packages")
        with open(os.path.join(pkg, "houdini_mcp_bridge.json")) as fh:
            hpath = json.load(fh)["hpath"]
        assert "~" not in hpath and os.path.isdir(hpath), hpath
        assert os.path.isfile(os.path.join(hpath, "python3.11libs", "pythonrc.py"))
    print("install OK: package files written for", versions)

    out = cli("install", env=env)
    assert "updated 1.0.0 -> 1.0.0" in out, out
    print("update OK")

    for hython, v in zip(HYTHONS, versions):
        script = os.path.join(root, "check.py")
        with open(script, "w") as fh:
            fh.write(CHECK)
        run = subprocess.run([hython, script], env=env, capture_output=True, text=True, timeout=300)
        line = [l for l in run.stdout.splitlines() if l.startswith("CHECK ")]
        assert line, run.stdout + run.stderr
        info = json.loads(line[0][6:])
        assert info["pythonrc_imported_bridge"], info
        assert ("houdini" + v) in info["bridge_file"].replace("\\", "/"), info
        assert not info["running"], "bridge must not autostart in hython"
        print("Houdini %s loaded the package, bridge not started in hython: OK" % info["version"])

    out = cli("uninstall", env=env)
    for v in versions:
        pkg = os.path.join(root, "houdini" + v, "packages")
        assert not os.listdir(pkg), os.listdir(pkg)
    print("uninstall OK")
    print("INSTALLER TEST OK")
finally:
    shutil.rmtree(root, ignore_errors=True)
