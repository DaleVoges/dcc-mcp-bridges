"""
Blender MCP server.

Exposes tools that let Claude drive a running Blender session through the
`claude_mcp_bridge` module running inside Blender (installed with `blender-mcp-bridge install`).

Run (stdio transport):  uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges blender-mcp-bridge
                        (or, from a checkout: python -m blender_mcp_bridge)
Env vars: BLENDER_MCP_HOST, BLENDER_MCP_PORT (first port scanned, default 7677),
          BLENDER_MCP_PORT_RANGE (default 10), BLENDER_MCP_TIMEOUT (seconds, default 600).
"""
from __future__ import annotations

from dcc_mcp_core import Bridge, BridgeError, Image, SessionGoneError, create_server

from blender_mcp_bridge import __version__

__all__ = ["BridgeError", "SessionGoneError", "bridge", "main", "mcp"]

INSTALL_COMMAND = "uvx --from git+https://github.com/DaleVoges/dcc-mcp-bridges blender-mcp-bridge install"

INSTRUCTIONS = """\
You are connected to a live Blender session.

Use `execute_python` for almost everything. The code runs inside Blender on its main
thread, in a namespace that persists between calls, with these names pre-imported:
  bpy, bmesh, mathutils, Vector, Matrix, Euler, Quaternion
  C -> bpy.context      D -> bpy.data
  view3d_override()  -> kwargs for bpy.context.temp_override() pointing at a 3D View
The value of the last expression (or a variable named `result`) is returned as JSON
(data-blocks come back as their names, vectors and matrices as lists). print() output is
returned too. Each call is one undo step, so the user can Ctrl+Z it.

Several Blenders can be open at once. If only one is running it is used automatically.
If several are, tools refuse to run until one is chosen: call `list_blender_sessions`, ask the
user which one (name it by its open .blend file), then `select_blender_session`. Never pick one
yourself. If a tool says the selected Blender is gone, nothing was run; confirm with the user
before selecting another.

Tips:
- Inspect before you modify: `get_scene_info`, `list_objects`, `inspect_object`,
  `inspect_node_tree` (materials, geometry nodes, world, compositor).
- Prefer the bpy.data API (bpy.data.objects.new, mesh.from_pydata, obj.modifiers.new,
  material.node_tree.nodes.new) over bpy.ops: operators depend on the UI context and often
  fail from a script. When you need one that wants a 3D View, wrap it:
  `with bpy.context.temp_override(**view3d_override()): bpy.ops....`
- Link new objects into a collection (e.g. C.scene.collection.objects.link(obj)) or they
  won't appear in the scene.
- Check exact property and operator argument names with `get_bpy_help`
  (e.g. "SubsurfModifier", "ops.mesh.primitive_cube_add", "ShaderNodeBsdfPrincipled").
- Use `capture_viewport` to look at the result.
- Do not open/save/revert .blend files, or delete large parts of the scene, unless the user asked.
"""


def _session_label(s: dict) -> str:
    return f"Blender {s.get('blender_version')}" + ("" if s.get("ui") else " (background)")


bridge = Bridge(
    "blender", "Blender", 7677, __version__, _session_label,
    not_found_hint="Ask the user to open Blender. If it is open, the bridge may not be "
                   f"installed: the user installs it by running `{INSTALL_COMMAND}` in a terminal "
                   "and restarting Blender.",
    update_hint=f"Tell the user to update it by running `{INSTALL_COMMAND}` and restarting Blender.",
)
mcp = create_server("blender", INSTRUCTIONS, __version__)


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
@mcp.tool()
def blender_status() -> str:
    """Reports the selected Blender: version, Python version, UI or background, port, pid, .blend file."""
    return bridge.status()


@mcp.tool()
def list_blender_sessions() -> str:
    """Lists every running Blender with the bridge started (port, version, pid, open .blend file).

    When several are running, ask the user which one to use and call select_blender_session.
    """
    return bridge.list_sessions()


@mcp.tool()
def select_blender_session(port: int) -> str:
    """Chooses which Blender the other tools talk to, by the port shown in list_blender_sessions."""
    return bridge.select_session(port)


@mcp.tool()
def execute_python(code: str, undoable: bool = True, reset_namespace: bool = False) -> str:
    """Executes Python code inside Blender, with bpy, bmesh, mathutils, C and D pre-imported.

    Variables persist between calls. The last expression's value (or a variable named
    `result`) is returned as JSON (data-blocks become their names, vectors/matrices lists),
    along with anything printed.

    Args:
        code: Python source to run. Multi-line is fine.
        undoable: Record the call as a single undo step (default True).
        reset_namespace: Clear variables left over from earlier calls first.
    """
    return bridge.run("python", code, undoable, reset_namespace)


@mcp.tool()
def get_scene_info() -> str:
    """Summarises the open file: .blend path, scenes, frame range, fps, render settings,
    objects per type, collections, selection and data-block counts."""
    return bridge.python(r'''
_sc = bpy.context.scene
_r = _sc.render
_types = {}
for _o in _sc.objects:
    _types[_o.type] = _types.get(_o.type, 0) + 1
def _coll_tree(c, depth=0):
    out = {"name": c.name, "objects": len(c.objects)}
    if c.children and depth < 6:
        out["children"] = [_coll_tree(ch, depth + 1) for ch in c.children]
    return out
_view_layer = bpy.context.view_layer
_active = _view_layer.objects.active
result = {
    "blend_file": bpy.data.filepath or None,
    "unsaved_changes": bpy.data.is_dirty,
    "blender": bpy.app.version_string,
    "scene": _sc.name,
    "scenes": [s.name for s in bpy.data.scenes],
    "view_layer": _view_layer.name,
    "frame_range": [_sc.frame_start, _sc.frame_end],
    "current_frame": _sc.frame_current,
    "fps": _r.fps / _r.fps_base,
    "render": {"engine": _r.engine, "resolution": [_r.resolution_x, _r.resolution_y],
               "resolution_percentage": _r.resolution_percentage,
               "output": _r.filepath},
    "camera": _sc.camera.name if _sc.camera else None,
    "world": _sc.world.name if _sc.world else None,
    "unit_system": _sc.unit_settings.system,
    "active_object": _active.name if _active else None,
    "selected_objects": [o.name for o in _view_layer.objects if o.select_get()][:200],
    "objects_by_type": _types,
    "collections": _coll_tree(_sc.collection),
    "data_counts": {k: len(getattr(bpy.data, k)) for k in (
        "objects", "meshes", "materials", "node_groups", "images", "textures", "actions",
        "cameras", "lights", "curves", "armatures", "collections")},
}
''')


@mcp.tool()
def list_objects(pattern: str = "*", object_type: str = "", collection: str = "",
                 selected_only: bool = False, limit: int = 500) -> str:
    """Lists objects in the current scene.

    Args:
        pattern: Object name wildcard, e.g. "Cube*" or "*_GEO".
        object_type: Blender object type, e.g. "MESH", "CURVE", "LIGHT", "CAMERA", "EMPTY",
            "ARMATURE", "GPENCIL"/"GREASEPENCIL", "VOLUME".
        collection: Only objects in this collection (and its children).
        selected_only: Only selected objects.
        limit: Maximum number of objects to return.
    """
    return bridge.python(rf'''
import fnmatch
if {collection!r}:
    _coll = bpy.data.collections.get({collection!r})
    if _coll is None:
        raise ValueError("No collection named %r" % {collection!r})
    _objs = list(_coll.all_objects)
else:
    _objs = list(bpy.context.scene.objects)
_hits = [o for o in _objs if fnmatch.fnmatchcase(o.name, {pattern!r})
         and (not {object_type!r} or o.type == {object_type!r}.upper())
         and (not {selected_only!r} or o.select_get())]
result = {{"count": len(_hits),
          "objects": [{{"name": o.name, "type": o.type,
                       "data": o.data.name if o.data else None,
                       "parent": o.parent.name if o.parent else None,
                       "collections": [c.name for c in o.users_collection],
                       "location": list(o.location),
                       "hidden": o.hide_get() if o.name in bpy.context.view_layer.objects else None}}
                      for o in _hits[:{int(limit)}]]}}
''')


@mcp.tool()
def inspect_object(name: str, mesh_stats: bool = True) -> str:
    """Describes an object: type, transforms, parent/children, collections, modifiers (with
    their settings), constraints, materials, animation, custom properties and, for meshes,
    vertex/face counts, attributes, UV maps and bounds.

    Args:
        name: Object name, e.g. "Cube".
        mesh_stats: For mesh objects, include counts of the evaluated (modified) mesh too.
    """
    return bridge.python(rf'''
_o = bpy.data.objects.get({name!r})
if _o is None:
    raise ValueError("No object named %r" % {name!r})

def _props(struct, skip=("rna_type", "name", "type", "show_expanded", "is_override_data_editable")):
    out = {{}}
    for _p in struct.bl_rna.properties:
        _k = _p.identifier
        if _k in skip or _p.type == "COLLECTION":
            continue
        try:
            _v = getattr(struct, _k)
        except Exception:
            continue
        if _p.type == "POINTER":
            _v = getattr(_v, "name", None) if _v is not None else None
        elif hasattr(_v, "__len__") and not isinstance(_v, str):
            _v = list(_v)
        out[_k] = _v
    return out

_info = {{
    "name": _o.name,
    "type": _o.type,
    "data": _o.data.name if _o.data else None,
    "parent": _o.parent.name if _o.parent else None,
    "parent_type": _o.parent_type if _o.parent else None,
    "children": [c.name for c in _o.children][:200],
    "collections": [c.name for c in _o.users_collection],
    "location": list(_o.location),
    "rotation_mode": _o.rotation_mode,
    "rotation_euler": list(_o.rotation_euler),
    "scale": list(_o.scale),
    "dimensions": list(_o.dimensions),
    "matrix_world": [list(r) for r in _o.matrix_world],
    "visible": {{"viewport": not _o.hide_viewport, "render": not _o.hide_render}},
    "modifiers": [dict(_props(m), name=m.name, type=m.type) for m in _o.modifiers],
    "constraints": [dict(_props(c), name=c.name, type=c.type) for c in _o.constraints],
    "materials": [s.material.name if s.material else None for s in _o.material_slots],
    "custom_properties": {{k: str(_o[k]) for k in _o.keys() if not k.startswith("_")}},
}}
_ad = _o.animation_data
if _ad is not None:
    _info["animation"] = {{
        "action": _ad.action.name if _ad.action else None,
        "drivers": [d.data_path + "[%d]" % d.array_index for d in _ad.drivers][:100],
        "nla_tracks": [t.name for t in _ad.nla_tracks],
    }}
if _o.type == "MESH":
    _me = _o.data
    _info["mesh"] = {{
        "vertices": len(_me.vertices), "edges": len(_me.edges), "faces": len(_me.polygons),
        "uv_maps": [u.name for u in _me.uv_layers],
        "attributes": [{{"name": a.name, "domain": a.domain, "type": a.data_type}}
                       for a in _me.attributes if not a.name.startswith(".")],
        "vertex_groups": [g.name for g in _o.vertex_groups],
        "shape_keys": [k.name for k in _me.shape_keys.key_blocks] if _me.shape_keys else [],
    }}
    if {mesh_stats!r} and _o.modifiers:
        _dg = bpy.context.evaluated_depsgraph_get()
        _ev = _o.evaluated_get(_dg)
        _em = _ev.to_mesh()
        try:
            _info["evaluated_mesh"] = {{"vertices": len(_em.vertices), "edges": len(_em.edges),
                                       "faces": len(_em.polygons)}}
        finally:
            _ev.to_mesh_clear()
elif _o.type == "CAMERA":
    _c = _o.data
    _info["camera"] = {{"type": _c.type, "lens": _c.lens, "sensor_width": _c.sensor_width,
                       "clip": [_c.clip_start, _c.clip_end], "dof": _c.dof.use_dof}}
elif _o.type == "LIGHT":
    _l = _o.data
    _info["light"] = {{"type": _l.type, "energy": _l.energy, "color": list(_l.color)}}
elif _o.data is not None and hasattr(_o.data, "bl_rna"):
    _info["data_settings"] = _props(_o.data)
_bb = [_o.matrix_world @ Vector(c) for c in _o.bound_box]
_info["world_bounds"] = {{"min": [min(v[i] for v in _bb) for i in range(3)],
                         "max": [max(v[i] for v in _bb) for i in range(3)]}}
result = _info
''')


@mcp.tool()
def inspect_node_tree(name: str, kind: str = "material", all_inputs: bool = False) -> str:
    """Describes a node tree: its nodes, their changed input values, and the links between them.

    Args:
        name: The material, node group, world or scene name (for kind="compositor").
        kind: "material", "node_group" (geometry nodes, shader groups...), "world" or "compositor".
        all_inputs: List every unlinked input, not just ones changed from their defaults.
    """
    return bridge.python(rf'''
_kind = {kind!r}
_name = {name!r}
_sources = {{"material": bpy.data.materials, "node_group": bpy.data.node_groups,
            "world": bpy.data.worlds, "compositor": bpy.data.scenes}}
if _kind not in _sources:
    raise ValueError("kind must be one of %s" % sorted(_sources))
_owner = _sources[_kind].get(_name)
if _owner is None:
    raise ValueError("No %s named %r. Known: %s" % (_kind, _name, [x.name for x in _sources[_kind]][:100]))
if _kind == "node_group":
    _tree = _owner
elif _kind == "compositor":
    _tree = getattr(_owner, "compositing_node_group", None) or _owner.node_tree
else:
    _tree = _owner.node_tree
if _tree is None:
    raise ValueError("%s %r has no node tree (use_nodes is off?)" % (_kind, _name))

def _val(v):
    if hasattr(v, "__len__") and not isinstance(v, str):
        return [round(x, 6) if isinstance(x, float) else x for x in v]
    if isinstance(v, float):
        return round(v, 6)
    if hasattr(v, "name"):
        return v.name
    return v

_defaults = {{}}  # node type -> {{input identifier: value}} on a freshly made node
if not {all_inputs!r}:
    _scratch = bpy.data.node_groups.new("_claude_defaults", _tree.bl_idname)
    try:
        for _bl in {{n.bl_idname for n in _tree.nodes}}:
            try:
                _fresh = _scratch.nodes.new(_bl)
            except Exception:
                continue
            _defaults[_bl] = {{s.identifier: _val(s.default_value) for s in _fresh.inputs
                              if hasattr(s, "default_value")}}
    finally:
        bpy.data.node_groups.remove(_scratch)

def _changed(node, sock):
    _d = _defaults.get(node.bl_idname)
    if _d is None or sock.identifier not in _d:
        return True
    return _val(sock.default_value) != _d[sock.identifier]

_nodes = []
for _n in _tree.nodes:
    _e = {{"name": _n.name, "type": _n.bl_idname, "label": _n.label or None,
          "location": [round(_n.location.x), round(_n.location.y)]}}
    _ins = {{}}
    for _s in _n.inputs:
        if _s.is_linked or not hasattr(_s, "default_value"):
            continue
        if {all_inputs!r} or _changed(_n, _s):
            try:
                _ins[_s.identifier if _s.identifier != _s.name else _s.name] = _val(_s.default_value)
            except Exception:
                pass
    if _ins:
        _e["inputs"] = _ins
    for _attr in ("operation", "data_type", "blend_type", "interpolation", "distribution",
                  "mode", "domain", "image", "node_tree"):
        if hasattr(_n, _attr):
            try:
                _e[_attr] = _val(getattr(_n, _attr))
            except Exception:
                pass
    _nodes.append(_e)
result = {{
    "tree": _tree.name, "tree_type": _tree.bl_idname,
    "nodes": _nodes,
    "links": ["%s.%s -> %s.%s" % (l.from_node.name, l.from_socket.identifier,
                                  l.to_node.name, l.to_socket.identifier)
              for l in _tree.links if l.is_valid],
}}
if hasattr(_tree, "interface"):
    result["interface"] = [{{"name": i.name, "in_out": getattr(i, "in_out", None),
                            "socket_type": getattr(i, "socket_type", None)}}
                           for i in _tree.interface.items_tree if i.item_type == "SOCKET"]
''')


@mcp.tool()
def get_bpy_help(name: str) -> str:
    """Lists the properties (or operator arguments) of a bpy type, with types, defaults and
    enum items. Use it to check exact names before setting them.

    Args:
        name: A bpy.types name ("Object", "SubsurfModifier", "ShaderNodeBsdfPrincipled",
            "GeometryNodeMeshToPoints"), an operator ("ops.mesh.primitive_cube_add" or
            "mesh.primitive_cube_add"), or any other Python path under bpy
            ("app.version", "utils.register_class") for its docstring.
    """
    return bridge.python(rf'''
import pydoc
_name = {name!r}.strip()
if _name.startswith("bpy."):
    _name = _name[4:]

def _describe(rna):
    _out = []
    for _p in rna.properties:
        if _p.identifier == "rna_type":
            continue
        _e = {{"name": _p.identifier, "label": _p.name, "type": _p.type}}
        if _p.description:
            _e["description"] = _p.description
        if _p.is_readonly:
            _e["readonly"] = True
        if _p.type in ("BOOLEAN", "INT", "FLOAT", "STRING"):
            _d = list(_p.default_array) if getattr(_p, "is_array", False) and _p.array_length else _p.default
            _e["default"] = _d
            if getattr(_p, "array_length", 0):
                _e["size"] = _p.array_length
        if _p.type in ("INT", "FLOAT"):
            _e["range"] = [_p.soft_min, _p.soft_max]
        if _p.type == "ENUM":
            _e["items"] = [i.identifier for i in _p.enum_items][:80]
            _e["default"] = sorted(_p.default_flag) if _p.is_enum_flag else _p.default
        if _p.type in ("POINTER", "COLLECTION"):
            _e["fixed_type"] = _p.fixed_type.identifier if _p.fixed_type else None
        _out.append(_e)
    return _out

_parts = _name.split(".")
if _parts[0] == "ops":
    _parts = _parts[1:]
if len(_parts) == 2 and hasattr(bpy.ops, _parts[0]) and _parts[0] not in ("types", "data", "app", "utils", "props", "path", "context"):
    _op = getattr(getattr(bpy.ops, _parts[0]), _parts[1])
    _rna = _op.get_rna_type()
    result = {{"operator": "bpy.ops." + ".".join(_parts), "description": _rna.description,
              "arguments": _describe(_rna)}}
elif len(_parts) == 1 or _parts[0] == "types":
    _tname = _parts[-1]
    _t = getattr(bpy.types, _tname, None)
    if _t is None:
        _hits = sorted(n for n in dir(bpy.types) if _tname.lower() in n.lower())
        result = {{"matches": _hits[:200], "note": "Pass one exact bpy.types name for details."}}
    else:
        _rna = _t.bl_rna
        result = {{"type": _tname, "description": _rna.description,
                  "base": _rna.base.identifier if _rna.base else None,
                  "properties": _describe(_rna),
                  "functions": [f.identifier for f in _rna.functions]}}
else:
    _obj = bpy
    for _part in _parts:
        _obj = getattr(_obj, _part)
    result = pydoc.render_doc(_obj, renderer=pydoc.plaintext)[:20000]
''')


@mcp.tool()
def capture_viewport(width: int = 960, height: int = 540, camera: str = "") -> Image:
    """Takes a picture of the Blender 3D View (a viewport render, as the user sees it).

    In background Blender (no UI) it renders through the scene camera with Workbench instead.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        camera: Optional camera object name to look through for the capture.
    """
    return bridge.image({"cmd": "capture", "width": width, "height": height,
                         "camera": camera or None})


def main() -> None:
    import sys
    args = sys.argv[1:]
    if args and args[0] in ("install", "uninstall"):
        import argparse
        from blender_mcp_bridge import install as installer
        parser = argparse.ArgumentParser(prog=f"blender-mcp-bridge {args[0]}")
        parser.add_argument("--config-dir", action="append", default=[],
                            help="Blender version config folder (e.g. .../Blender/4.5). "
                                 "Repeatable. Default: every one found.")
        opts = parser.parse_args(args[1:])
        (installer.install if args[0] == "install" else installer.uninstall)(opts.config_dir)
        return
    if args and args[0] in ("--version", "-V"):
        print(__version__)
        return
    mcp.run()


if __name__ == "__main__":
    main()
