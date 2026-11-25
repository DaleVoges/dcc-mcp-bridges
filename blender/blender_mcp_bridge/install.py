"""Installs the Blender side of the bridge as a startup script.

    blender-mcp-bridge install                   # every Blender version found
    blender-mcp-bridge install --config-dir DIR  # one version folder, e.g. .../Blender/4.5
    blender-mcp-bridge uninstall

For each Blender version folder this writes:
    <version>/scripts/startup/claude_mcp_bridge.py

Blender imports every module in scripts/startup when it starts and calls its register(),
which starts the bridge in graphical Blender. No add-on needs to be enabled.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from blender_mcp_bridge import __version__

MODULE = "claude_mcp_bridge.py"
SOURCE = Path(__file__).resolve().parent / "blender_addon" / MODULE
_VERSION_DIR = re.compile(r"^\d+\.\d+$")


def _long_path(path: str) -> str:
    """Expands Windows 8.3 short names (DALE~1.VOG) so the printed path is readable."""
    if sys.platform.startswith("win"):
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetLongPathNameW(str(path), buf, 1024):
            return buf.value
    return str(path)


def config_root() -> Path:
    """The folder holding one subfolder per Blender version (4.5, 5.0, ...)."""
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Blender Foundation" / "Blender"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Blender"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "blender"


def find_version_dirs() -> list[Path]:
    """Every Blender version folder on this machine (Blender creates one per version)."""
    root = config_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if _VERSION_DIR.match(p.name) and p.is_dir())


def startup_dir(version_dir: Path) -> Path:
    return version_dir / "scripts" / "startup"


def installed_version(version_dir: Path) -> str | None:
    bridge = startup_dir(version_dir) / MODULE
    if not bridge.is_file():
        return None
    match = re.search(r'^BRIDGE_VERSION\s*=\s*"([^"]+)"', bridge.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "unknown"


def install_into(version_dir: Path) -> Path:
    dest = startup_dir(version_dir)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, dest / MODULE)
    return dest / MODULE


def uninstall_from(version_dir: Path) -> bool:
    target = startup_dir(version_dir) / MODULE
    if not target.exists():
        return False
    target.unlink()
    cache = startup_dir(version_dir) / "__pycache__"
    for compiled in cache.glob("claude_mcp_bridge.*.pyc") if cache.is_dir() else []:
        compiled.unlink()
    return True


def _targets(config_dirs: list[str]) -> list[Path]:
    if config_dirs:
        return [Path(p) for p in config_dirs]
    found = find_version_dirs()
    if not found:
        sys.exit(f"No Blender config folder found in {config_root()}. Start Blender once (it "
                 "creates the folder), then run this again, or pass --config-dir <path to the "
                 "version folder, e.g. .../Blender/4.5>.")
    return found


def install(config_dirs: list[str]) -> None:
    for version_dir in _targets(config_dirs):
        old = installed_version(version_dir)
        dest = install_into(version_dir)
        action = f"updated {old} -> {__version__}" if old else f"installed {__version__}"
        print(f"Blender {version_dir.name}: {action}  ({_long_path(str(dest))})")
    print("""
Done. Restart Blender: the bridge starts with it, and the system console
(Window > Toggle System Console on Windows) shows
"[Claude MCP] Bridge listening on 127.0.0.1:7677".

To start it in a Blender that is already open, paste this into the Python Console:
    import claude_mcp_bridge; claude_mcp_bridge.start()""")


def uninstall(config_dirs: list[str]) -> None:
    targets = [Path(p) for p in config_dirs] if config_dirs else find_version_dirs()
    removed = [p for p in targets if uninstall_from(p)]
    for version_dir in removed:
        print(f"Blender {version_dir.name}: removed")
    if not removed:
        print("Nothing to remove: the bridge is not installed in any Blender config folder found.")
    else:
        print("Restart Blender to unload it.")
