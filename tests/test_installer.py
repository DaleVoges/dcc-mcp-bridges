"""Tests `nuke-mcp-bridge install/uninstall` against a throwaway .nuke folder.

    python tests/test_installer.py <Nuke.exe> [<another Nuke.exe> ...]

Points HOME at a temp folder (Nuke reads $HOME/.nuke), with an existing init.py, then checks
that each Nuke (terminal mode) loads the plug-in folder without starting the bridge, and that
uninstall restores init.py exactly.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
NUKES = sys.argv[1:]

USER_INIT = b"import nuke\r\n\r\nnuke.pluginAddPath('my_studio_tools')\r\n"
CHECK = r'''
import sys, json, nuke
try:
    import claude_mcp_bridge as b
    info = {"importable": True, "file": b.__file__, "running": b.is_running()}
except ImportError as exc:
    info = {"importable": False, "error": str(exc)}
info["version"] = nuke.NUKE_VERSION_STRING
info["plugin_path"] = [p for p in nuke.pluginPath() if "nuke_mcp_bridge" in p]
print("CHECK " + json.dumps(info))
'''


def cli(*args, env):
    out = subprocess.run([sys.executable, "-m", "nuke_mcp_bridge", *args], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


home = tempfile.mkdtemp(prefix="nmcp_home_")
try:
    nuke_dir = os.path.join(home, ".nuke")
    os.makedirs(os.path.join(nuke_dir, "my_studio_tools"))
    init_py = os.path.join(nuke_dir, "init.py")
    with open(init_py, "wb") as fh:
        fh.write(USER_INIT)
    env = dict(os.environ, PYTHONPATH=SRC, HOME=home)

    print(cli("install", env=env).strip().splitlines()[0])
    cli("install", env=env)  # again: must not add a second block
    with open(init_py, "rb") as fh:
        text = fh.read()
    assert text.startswith(USER_INIT), text
    assert text.count(b">>> nuke-mcp-bridge >>>") == 1, text
    assert b"\r\n" in text.split(USER_INIT, 1)[1], "line endings not kept"
    print("install OK: init.py keeps the user's lines, one bridge block, CRLF kept")

    script = os.path.join(home, "check.py")
    with open(script, "w") as fh:
        fh.write(CHECK)
    for exe in NUKES:
        run = subprocess.run([exe, "-t", script], env=env, capture_output=True, text=True,
                             timeout=300)
        line = [l for l in run.stdout.splitlines() if l.startswith("CHECK ")]
        assert line, run.stdout + run.stderr
        info = json.loads(line[0][6:])
        assert info["importable"], info
        assert not info["running"], "bridge must not start in terminal mode"
        assert info["plugin_path"], info
        print("Nuke %s loaded the plug-in, bridge not started in terminal mode: OK" % info["version"])

    cli("uninstall", env=env)
    with open(init_py, "rb") as fh:
        assert fh.read() == USER_INIT, "init.py not restored exactly"
    assert not os.path.exists(os.path.join(nuke_dir, "nuke_mcp_bridge"))
    print("uninstall OK: init.py restored byte for byte")
    print("INSTALLER TEST OK")
finally:
    shutil.rmtree(home, ignore_errors=True)
