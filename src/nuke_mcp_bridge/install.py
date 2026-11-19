"""Installs the Nuke side of the bridge into your .nuke folder (shared by every Nuke version).

    nuke-mcp-bridge install                  # ~/.nuke
    nuke-mcp-bridge install --nuke-dir DIR   # another .nuke folder
    nuke-mcp-bridge uninstall

Writes:
    <.nuke>/nuke_mcp_bridge/      claude_mcp_bridge.py + menu.py (starts the bridge in the GUI)
    <.nuke>/init.py               adds a marked block: nuke.pluginAddPath("nuke_mcp_bridge")
Everything else in init.py is left alone; uninstall removes only the marked block.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from nuke_mcp_bridge import __version__

FOLDER = "nuke_mcp_bridge"
SOURCE = Path(__file__).resolve().parent / "nuke_plugin"
BEGIN = "# >>> nuke-mcp-bridge >>>"
END = "# <<< nuke-mcp-bridge <<<"
BLOCK = f'''{BEGIN}
# Added by nuke-mcp-bridge. Remove with: nuke-mcp-bridge uninstall
import nuke
nuke.pluginAddPath("{FOLDER}")
{END}
'''
_BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)


def default_nuke_dir() -> Path:
    """Nuke uses $HOME/.nuke (on Windows %HOME% if set, else %USERPROFILE%)."""
    home = os.environ.get("HOME") if sys.platform.startswith("win") else None
    return Path(home or Path.home()) / ".nuke"


def installed_version(nuke_dir: Path) -> str | None:
    bridge = nuke_dir / FOLDER / "claude_mcp_bridge.py"
    if not bridge.is_file():
        return None
    match = re.search(r'^BRIDGE_VERSION\s*=\s*"([^"]+)"', bridge.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "unknown"


def _read(path: Path) -> tuple[str, str]:
    """Returns (text with \\n line endings, the file's own line ending)."""
    if not path.is_file():
        return "", os.linesep
    raw = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), eol


def _write(path: Path, text: str, eol: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", eol))


def install_into(nuke_dir: Path) -> Path:
    nuke_dir.mkdir(parents=True, exist_ok=True)
    dest = nuke_dir / FOLDER
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    init_py = nuke_dir / "init.py"
    text, eol = _read(init_py)
    text = _BLOCK_RE.sub("", text).rstrip("\n")
    _write(init_py, (text + "\n\n" if text.strip() else "") + BLOCK, eol)
    return dest


def uninstall_from(nuke_dir: Path) -> bool:
    removed = False
    if (nuke_dir / FOLDER).exists():
        shutil.rmtree(nuke_dir / FOLDER)
        removed = True
    init_py = nuke_dir / "init.py"
    text, eol = _read(init_py)
    if BEGIN in text:
        text = _BLOCK_RE.sub("", text).rstrip("\n")
        _write(init_py, text + "\n" if text.strip() else "", eol)
        removed = True
    return removed


def install(nuke_dir: str | None) -> None:
    target = Path(nuke_dir) if nuke_dir else default_nuke_dir()
    old = installed_version(target)
    dest = install_into(target)
    action = f"updated {old} -> {__version__}" if old else f"installed {__version__}"
    print(f"Nuke bridge {action}  ({dest})")
    print(f"""
Done. This covers every Nuke version. Restart Nuke: the bridge starts with it, and the
Script Editor shows "[Claude MCP] Bridge listening on 127.0.0.1:7577".
Edit > Claude MCP has Start/Stop items.

To start it in a Nuke that is already open, paste this into the Script Editor:
    import sys; sys.path.append(r"{dest}"); import claude_mcp_bridge; claude_mcp_bridge.start()""")


def uninstall(nuke_dir: str | None) -> None:
    target = Path(nuke_dir) if nuke_dir else default_nuke_dir()
    if uninstall_from(target):
        print(f"Removed the Nuke bridge from {target}. Restart Nuke to unload it.")
    else:
        print(f"Nothing to remove: the bridge is not installed in {target}.")
