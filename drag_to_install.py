# -*- coding: utf-8 -*-
"""
Maya MCP Bridge installer.

Drag this file into any Maya viewport. It:
  1. copies the plug-in into <Maya prefs>/modules/maya_mcp_bridge/
  2. writes maya_mcp_bridge.mod so every Maya version finds it
  3. loads the plug-in now and sets it to auto-load

Drag it in again to update or uninstall. Works in Maya 2018+ (Python 2.7 and 3).
"""
from __future__ import print_function

import inspect
import os
import re
import shutil

import maya.cmds as cmds
import maya.mel as mel

MODULE_NAME = "maya_mcp_bridge"
MOD_FILE = MODULE_NAME + ".mod"
PLUGIN = "claude_mcp_bridge"
LEGACY_MOD_FILES = ("claude_mcp.mod",)  # written by the old install.ps1
SETUP_COMMAND = "claude mcp add maya -- uvx maya-mcp-bridge"
README_URL = "https://github.com/DaleVoges/maya-mcp-bridge#connect-claude"


def _here():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:  # some Maya versions exec dropped files without __file__
        return os.path.dirname(os.path.abspath(inspect.getsourcefile(_here)))


def _long_path(path):
    """Expands Windows 8.3 short names (e.g. DALE~1.VOG): Maya can't find plug-ins under them."""
    if os.name != "nt" or "~" not in path:
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetLongPathNameW(u"%s" % path, buf, len(buf)):
            return buf.value
    except Exception:
        pass
    return path


def _modules_dir():
    path = os.path.join(cmds.internalVar(userAppDir=True), "modules")
    if not os.path.isdir(path):
        os.makedirs(path)
    return _long_path(os.path.normpath(path)).replace("\\", "/")


def _plugin_source(src_dir):
    return os.path.join(src_dir, "maya_module", "plug-ins", PLUGIN + ".py")


def _read_version(plugin_file):
    with open(plugin_file) as fh:
        match = re.search(r'^PLUGIN_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    return match.group(1) if match else "0.0.0"


def installed_version():
    """Version recorded in the installed .mod file, or None."""
    mod_path = os.path.join(_modules_dir(), MOD_FILE)
    if not os.path.isfile(mod_path):
        return None
    with open(mod_path) as fh:
        match = re.search(r"^\+\s+\S+\s+(\S+)", fh.read(), re.M)
    return match.group(1) if match else "unknown"


def _unload():
    if cmds.pluginInfo(PLUGIN, query=True, loaded=True):
        cmds.unloadPlugin(PLUGIN, force=True)


def _add_to_session_plugin_path(path):
    """The .mod file is only read at startup, so expose the plug-in to this session too."""
    current = os.environ.get("MAYA_PLUG_IN_PATH", "")
    if path not in current.split(os.pathsep):
        new = path + os.pathsep + current if current else path
        os.environ["MAYA_PLUG_IN_PATH"] = new
        mel.eval('putenv "MAYA_PLUG_IN_PATH" "%s"' % new.replace("\\", "/"))


def install(src_dir=None, load=True):
    """Installs (or updates) the module. Returns the installed version."""
    src_dir = src_dir or _here()
    plugin_src = _plugin_source(src_dir)
    if not os.path.isfile(plugin_src):
        raise RuntimeError("Can't find %s. Keep drag_to_install.py next to the maya_module folder "
                           "from the download." % plugin_src)
    version = _read_version(plugin_src)

    modules_dir = _modules_dir()
    target = modules_dir + "/" + MODULE_NAME
    plugins_dir = target + "/plug-ins"

    _unload()
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(plugins_dir)
    shutil.copy2(plugin_src, plugins_dir)

    with open(os.path.join(modules_dir, MOD_FILE), "w") as fh:
        fh.write("+ MayaMCPBridge %s %s\n" % (version, target))

    for legacy in LEGACY_MOD_FILES:
        legacy_path = os.path.join(modules_dir, legacy)
        if os.path.isfile(legacy_path):
            os.remove(legacy_path)
            print("[Maya MCP Bridge] Removed old %s" % legacy_path)

    _add_to_session_plugin_path(plugins_dir)
    if load:
        cmds.loadPlugin(plugins_dir + "/" + PLUGIN + ".py")
        cmds.pluginInfo(PLUGIN, edit=True, autoload=True)
    print("[Maya MCP Bridge] Installed %s to %s" % (version, target))
    return version


def uninstall():
    if cmds.pluginInfo(PLUGIN, query=True, loaded=True):
        cmds.pluginInfo(PLUGIN, edit=True, autoload=False)
    _unload()
    modules_dir = _modules_dir()
    target = modules_dir + "/" + MODULE_NAME
    if os.path.isdir(target):
        shutil.rmtree(target)
    mod_path = os.path.join(modules_dir, MOD_FILE)
    if os.path.isfile(mod_path):
        os.remove(mod_path)
    print("[Maya MCP Bridge] Uninstalled")


def _copy_to_clipboard(text):
    for qt in ("PySide6", "PySide2", "PySide"):
        try:
            widgets = __import__(qt + ".QtWidgets" if qt != "PySide" else qt + ".QtGui",
                                 fromlist=["QApplication"])
            widgets.QApplication.clipboard().setText(text)
            return True
        except Exception:
            continue
    return False


def _run_interactive():
    title = "Maya MCP Bridge"
    new_version = _read_version(_plugin_source(_here()))
    current = installed_version()

    if current:
        choice = cmds.confirmDialog(
            title=title,
            message="Maya MCP Bridge %s is installed.\nThis installer has version %s."
                    % (current, new_version),
            button=["Update", "Uninstall", "Cancel"], defaultButton="Update",
            cancelButton="Cancel", dismissString="Cancel")
        if choice == "Uninstall":
            uninstall()
            cmds.confirmDialog(title=title, message="Uninstalled.", button=["OK"])
            return
        if choice != "Update":
            return

    try:
        version = install()
    except Exception as exc:
        cmds.confirmDialog(title=title, message="Install failed:\n\n%s" % exc, button=["OK"])
        raise

    choice = cmds.confirmDialog(
        title=title,
        message=("Maya MCP Bridge %s is installed and running.\n"
                 "It will load automatically every time Maya starts.\n\n"
                 "Last step: connect Claude. For Claude Code, run:\n\n"
                 "    %s\n\n"
                 "For Claude Desktop, see the README." % (version, SETUP_COMMAND)),
        button=["Copy command", "Open README", "Close"], defaultButton="Copy command",
        cancelButton="Close", dismissString="Close")
    if choice == "Copy command":
        if not _copy_to_clipboard(SETUP_COMMAND):
            print(SETUP_COMMAND)
    elif choice == "Open README":
        cmds.launch(webPage=README_URL)


def onMayaDroppedPythonFile(*_args):
    """Called by Maya when this file is dropped into a viewport."""
    _run_interactive()
