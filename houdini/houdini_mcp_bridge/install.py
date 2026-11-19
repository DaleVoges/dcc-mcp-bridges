"""Installs the Houdini side of the bridge as a Houdini package.

    houdini-mcp-bridge install                  # every Houdini version found
    houdini-mcp-bridge install --prefs-dir DIR  # one prefs folder, e.g. .../houdini21.0
    houdini-mcp-bridge uninstall

For each Houdini prefs folder this writes:
    <prefs>/packages/houdini_mcp_bridge.json   package file (absolute path to the folder below)
    <prefs>/packages/houdini_mcp_bridge/       scripts/python/claude_mcp_bridge.py
                                               python3.Xlibs/pythonrc.py (starts the bridge)
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
from pathlib import Path

from houdini_mcp_bridge import __version__

PACKAGE_NAME = "houdini_mcp_bridge"
SOURCE = Path(__file__).resolve().parent / "houdini_package"
# Houdini only runs pythonrc.py from the folder matching its own Python version.
PYTHON_VERSIONS = ["3.7", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]
PYTHONRC = '''\
# Installed by houdini-mcp-bridge {version}. Starts the Claude MCP bridge in graphical Houdini.
try:
    import claude_mcp_bridge
    claude_mcp_bridge.autostart()
except Exception as exc:
    print("[Claude MCP] Bridge not started: %s" % exc)
'''
_VERSION_DIR = re.compile(r"^houdini(\d+\.\d+)$")


def _long_path(path: str) -> str:
    """Expands Windows 8.3 short names (DALE~1.VOG) so Houdini gets a normal path."""
    if sys.platform.startswith("win"):
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetLongPathNameW(str(path), buf, 1024):
            return buf.value
    return str(path)


def _documents_dir() -> Path:
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.shell32.SHGetFolderPathW(0, 5, 0, 0, buf) == 0:  # CSIDL_PERSONAL
        return Path(buf.value)
    return Path.home() / "Documents"


def find_prefs_dirs() -> list[Path]:
    """Every Houdini user prefs folder on this machine (Houdini creates one per version)."""
    custom = os.environ.get("HOUDINI_USER_PREF_DIR")
    if custom:
        if "__HVER__" not in custom:
            return [Path(custom)]
        pattern = re.escape(custom).replace("__HVER__", r"\d+\.\d+")
        hits = glob.glob(custom.replace("__HVER__", "*"))
        return sorted(Path(h) for h in hits if re.fullmatch(pattern, h) and os.path.isdir(h))

    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Preferences" / "houdini"
        return sorted(p for p in root.glob("*") if re.fullmatch(r"\d+\.\d+", p.name) and p.is_dir())

    roots = [Path.home()]
    if sys.platform.startswith("win"):
        roots = [_documents_dir()]
        if os.environ.get("HOME"):  # Houdini uses %HOME% instead of Documents when it's set
            roots.append(Path(os.environ["HOME"]))
    found = []
    for root in roots:
        if root.is_dir():
            found += [p for p in root.iterdir() if _VERSION_DIR.match(p.name) and p.is_dir()]
    return sorted(set(found))


def installed_version(prefs: Path) -> str | None:
    bridge = prefs / "packages" / PACKAGE_NAME / "scripts" / "python" / "claude_mcp_bridge.py"
    if not bridge.is_file():
        return None
    match = re.search(r'^BRIDGE_VERSION\s*=\s*"([^"]+)"', bridge.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "unknown"


def install_into(prefs: Path) -> Path:
    packages = prefs / "packages"
    dest = packages / PACKAGE_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for version in PYTHON_VERSIONS:
        libs = dest / f"python{version}libs"
        libs.mkdir(parents=True, exist_ok=True)
        (libs / "pythonrc.py").write_text(PYTHONRC.format(version=__version__), encoding="utf-8")
    package_file = {
        "hpath": _long_path(str(dest)).replace("\\", "/"),
        "load_package_once": True,
    }
    (packages / f"{PACKAGE_NAME}.json").write_text(json.dumps(package_file, indent=4) + "\n",
                                                   encoding="utf-8")
    return dest


def uninstall_from(prefs: Path) -> bool:
    packages = prefs / "packages"
    removed = False
    if (packages / PACKAGE_NAME).exists():
        shutil.rmtree(packages / PACKAGE_NAME)
        removed = True
    if (packages / f"{PACKAGE_NAME}.json").exists():
        (packages / f"{PACKAGE_NAME}.json").unlink()
        removed = True
    return removed


def _targets(prefs_dirs: list[str]) -> list[Path]:
    if prefs_dirs:
        return [Path(p) for p in prefs_dirs]
    found = find_prefs_dirs()
    if not found:
        sys.exit("No Houdini preferences folder found. Start Houdini once (it creates the folder), "
                 "then run this again, or pass --prefs-dir <path to houdiniX.Y>.")
    return found


def install(prefs_dirs: list[str]) -> None:
    for prefs in _targets(prefs_dirs):
        old = installed_version(prefs)
        dest = install_into(prefs)
        action = f"updated {old} -> {__version__}" if old else f"installed {__version__}"
        print(f"{prefs.name}: {action}  ({_long_path(str(dest))})")
    scripts = Path(_long_path(str(dest / "scripts" / "python"))).as_posix()
    print(f"""
Done. Restart Houdini: the bridge starts with it, and the Houdini console shows
"[Claude MCP] Bridge listening on 127.0.0.1:7477".

To start it in a Houdini that is already open, paste this into Windows > Python Shell:
    import sys; sys.path.append("{scripts}"); import claude_mcp_bridge; claude_mcp_bridge.start()""")


def uninstall(prefs_dirs: list[str]) -> None:
    targets = [Path(p) for p in prefs_dirs] if prefs_dirs else find_prefs_dirs()
    removed = [p for p in targets if uninstall_from(p)]
    for prefs in removed:
        print(f"{prefs.name}: removed")
    if not removed:
        print("Nothing to remove: the bridge is not installed in any Houdini prefs folder found.")
    else:
        print("Restart Houdini to unload it.")
