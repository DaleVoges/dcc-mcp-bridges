"""Tests `blender-mcp-bridge install/uninstall` against a throwaway config folder.

    python tests/test_installer.py <blender.exe> [<another blender.exe> ...]

Points the installer's config lookup (APPDATA on Windows, XDG_CONFIG_HOME on Linux, HOME on
macOS) at a temp folder with one version folder per Blender given, installs, then checks in
each Blender (background mode, BLENDER_USER_RESOURCES set to that version folder) that it
loaded the startup script without starting the bridge.
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
BLENDERS = sys.argv[1:]

CHECK = r'''
import sys, json, bpy
mod = sys.modules.get("claude_mcp_bridge")
print("CHECK " + json.dumps({
    "version": bpy.app.version_string,
    "startup_loaded_bridge": mod is not None,
    "bridge_file": getattr(mod, "__file__", None),
    "running": bool(mod and mod.is_running()),
}))
'''


def cli(*args, env):
    out = subprocess.run([sys.executable, "-m", "blender_mcp_bridge", *args], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def blender_version(exe):
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    return re.search(r"Blender (\d+\.\d+)", out).group(1)


root = tempfile.mkdtemp(prefix="bmcp_install_")
try:
    if sys.platform.startswith("win"):
        config = os.path.join(root, "Blender Foundation", "Blender")
        env = dict(os.environ, PYTHONPATH=SRC, APPDATA=root)
    elif sys.platform == "darwin":
        config = os.path.join(root, "Library", "Application Support", "Blender")
        env = dict(os.environ, PYTHONPATH=SRC, HOME=root)
    else:
        config = os.path.join(root, "blender")
        env = dict(os.environ, PYTHONPATH=SRC, XDG_CONFIG_HOME=root)
    versions = [blender_version(b) for b in BLENDERS]
    for v in versions:
        os.makedirs(os.path.join(config, v))

    out = cli("install", env=env)
    print(out.strip().splitlines()[0])
    for v in versions:
        assert os.path.isfile(os.path.join(config, v, "scripts", "startup", "claude_mcp_bridge.py"))
    print("install OK: startup script written for", versions)

    out = cli("install", env=env)
    assert "updated 1.0.0 -> 1.0.0" in out, out
    print("update OK")

    script = os.path.join(root, "check.py")
    with open(script, "w") as fh:
        fh.write(CHECK)
    for exe, v in zip(BLENDERS, versions):
        # Blender finds its config folder through the OS (not APPDATA), so point it at the
        # temp version folder explicitly. The installer itself found it through `env` above.
        run_env = dict(env, BLENDER_USER_RESOURCES=os.path.join(config, v))
        run = subprocess.run([exe, "--background", "--python", script], env=run_env,
                             capture_output=True, text=True, timeout=300)
        line = [l for l in run.stdout.splitlines() if l.startswith("CHECK ")]
        assert line, run.stdout + run.stderr
        info = json.loads(line[0][6:])
        assert info["startup_loaded_bridge"], info
        assert info["bridge_file"].replace("\\", "/").startswith(config.replace("\\", "/")), info
        assert not info["running"], "bridge must not autostart in background mode"
        print("Blender %s loaded the startup script, bridge not started in background: OK"
              % info["version"])

    cli("uninstall", env=env)
    for v in versions:
        startup = os.path.join(config, v, "scripts", "startup")
        assert not [f for f in os.listdir(startup) if f.startswith("claude_mcp_bridge")], os.listdir(startup)
    print("uninstall OK")
    print("INSTALLER TEST OK")
finally:
    shutil.rmtree(root, ignore_errors=True)
