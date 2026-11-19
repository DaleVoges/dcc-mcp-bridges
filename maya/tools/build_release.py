"""Builds the Maya download into dist/maya-mcp-bridge-<version>.zip.

    python tools/build_release.py

The zip holds drag_to_install.py and maya_module/. The server isn't in it: Claude installs
that straight from the GitHub repo with uvx.
"""
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")


def _version(path, pattern):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        match = re.search(pattern, fh.read(), re.M)
    if not match:
        sys.exit(f"No version found in {path}")
    return match.group(1)


def main():
    plugin_v = _version("maya_module/plug-ins/claude_mcp_bridge.py", r'^PLUGIN_VERSION\s*=\s*"([^"]+)"')
    package_v = _version("src/maya_mcp_bridge/__init__.py", r'^__version__\s*=\s*"([^"]+)"')
    if plugin_v != package_v:
        sys.exit(f"Version mismatch: plug-in {plugin_v} vs package {package_v}. Bump both.")

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    folder = f"maya-mcp-bridge-{plugin_v}"
    zip_path = os.path.join(DIST, folder + ".zip")
    files = ["drag_to_install.py", "README.md", "maya_module/plug-ins/claude_mcp_bridge.py"]
    if os.path.isfile(os.path.join(ROOT, "LICENSE")):
        files.append("LICENSE")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(os.path.join(ROOT, rel), f"{folder}/{rel}")
    print("built", zip_path)


if __name__ == "__main__":
    main()
