"""
Procedural geometry recipe library.

Each recipe is a chunk of bpy source that defines

    def _build_geometry(name, color_hex, detail, seed=0):
        ...            # create + shape real geometry
        return obj

and is injected verbatim into the live-backend script (mcp_backend.py) — so the
recipe code can use normal braces/dicts without `.format` escaping.

These are *modifier + procedural-displacement* recipes (Subdivision, Displace
with a procedural texture, Bevel, Solidify, Decimate, Skin…), which are robust
across Blender versions and produce genuinely detailed, parametric geometry —
unlike the bare primitives the first cut used. They are the practical stand-in
for a shipped `.blend` library of authored Geometry-Nodes groups: same role
(named, parameterised generators the LLM selects), reliable to emit from code.

`detail` is the LLM-facing 1–5 knob: it scales subdivision level, displacement
strength, and instance counts. Geometry only — colour/material is applied
separately by the palette material step (see mcp_backend._MATERIAL_SRC).

Recipe coverage (procedural-friendly asset types):
    terrain_noise   — displaced ground plane
    rock_kit        — eroded boulder (ico-sphere + displace + decimate)
    crystal_cluster — faceted crystal shards
    foliage_tree    — trunk + canopy
    building_kit    — beveled block + roof
    scatter_kit     — a small ring of mixed props
    prop_kit        — rounded hard-surface prop (fallback)
    humanoid_base   — capsule-ish biped placeholder (characters/creatures)
"""

from __future__ import annotations

# asset_type / geometry_nodes name → recipe key
_DISPATCH: dict[str, str] = {
    "terrain":     "terrain_noise",
    "environment": "terrain_noise",
    "foliage":     "foliage_tree",
    "building":    "building_kit",
    "prop":        "rock_kit",
    "weapon":      "prop_kit",
    "vehicle":     "prop_kit",
    "character":   "humanoid_base",
    "creature":    "humanoid_base",
    "material":    "prop_kit",
    # explicit geometry_nodes recipe names route straight through:
    "terrain_noise": "terrain_noise", "rock_kit": "rock_kit",
    "crystal_cluster": "crystal_cluster", "foliage_tree": "foliage_tree",
    "foliage_scatter": "foliage_tree", "building_kit": "building_kit",
    "scatter_kit": "scatter_kit", "prop_kit": "prop_kit",
    "human_base": "humanoid_base", "creature_base": "humanoid_base",
    "vehicle_base": "prop_kit",
}

RECIPE_NAMES = sorted(set(_DISPATCH.values()))


def recipe_for(asset_type: str, geometry_nodes: str = "") -> str:
    """Return the recipe key to use, preferring an explicit geometry_nodes name."""
    if geometry_nodes and geometry_nodes in _DISPATCH:
        return _DISPATCH[geometry_nodes]
    return _DISPATCH.get(asset_type, "prop_kit")


def recipe_source(recipe_key: str) -> str:
    """The bpy source defining `_build_geometry` for `recipe_key`."""
    return _RECIPES.get(recipe_key, _RECIPES["prop_kit"])


# Shared helpers prepended to every recipe (displacement texture, smooth shade).
_PRELUDE = '''
def _disp_texture(name, kind, size):
    tex = bpy.data.textures.get(name) or bpy.data.textures.new(name, type=kind)
    try:
        tex.noise_scale = size
    except Exception:
        pass
    return tex

def _shade_smooth(obj):
    for p in obj.data.polygons:
        p.use_smooth = True

def _spawn(name):
    # remove a prior run's object so re-runs are idempotent
    old = bpy.data.objects.get(name)
    if old is not None:
        bpy.data.objects.remove(old, do_unlink=True)
'''

# Each recipe ends with `return obj`; SPAWN_X/_collection come from the host script.
_RECIPES: dict[str, str] = {

"terrain_noise": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=2, size=20.0,
                                    location=(SPAWN_X, 0, 0))
    obj = bpy.context.active_object
    obj.name = name
    sub = obj.modifiers.new("Subsurf", "SUBSURF")
    sub.subdivision_type = "SIMPLE"
    sub.levels = sub.render_levels = min(6, 2 + detail)
    disp = obj.modifiers.new("Displace", "DISPLACE")
    disp.texture = _disp_texture(name + "_terr", "CLOUDS", 0.6)
    disp.strength = 1.0 + detail * 0.8
    _shade_smooth(obj)
    return obj
''',

"rock_kit": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=min(5, 2 + detail), radius=1.0,
                                          location=(SPAWN_X, 0, 1))
    obj = bpy.context.active_object
    obj.name = name
    disp = obj.modifiers.new("Erode", "DISPLACE")
    t = _disp_texture(name + "_rock", "VORONOI", 0.8)
    disp.texture = t
    disp.strength = 0.4 + detail * 0.25
    dec = obj.modifiers.new("Decimate", "DECIMATE")
    dec.ratio = max(0.3, 0.9 - detail * 0.08)
    _shade_smooth(obj)
    return obj
''',

"crystal_cluster": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    import random
    rng = random.Random(seed)
    bpy.ops.mesh.primitive_cone_add(vertices=6, radius1=0.5, radius2=0.0, depth=2.0,
                                    location=(SPAWN_X, 0, 1))
    obj = bpy.context.active_object
    obj.name = name
    shards = max(3, detail + 2)
    for i in range(shards):
        bpy.ops.mesh.primitive_cone_add(vertices=6, radius1=0.18 + rng.random() * 0.2,
                                        radius2=0.0, depth=0.8 + rng.random() * 1.6,
                                        location=(SPAWN_X + rng.uniform(-0.6, 0.6),
                                                  rng.uniform(-0.6, 0.6),
                                                  0.4 + rng.random() * 0.6))
        s = bpy.context.active_object
        s.rotation_euler = (rng.uniform(-0.4, 0.4), rng.uniform(-0.4, 0.4), rng.uniform(0, 3.14))
        s.select_set(True)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    _shade_smooth(obj)
    return obj
''',

"foliage_tree": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.18, depth=2.2,
                                        location=(SPAWN_X, 0, 1.1))
    trunk = bpy.context.active_object
    trunk.name = name
    canopy_layers = max(2, detail)
    for i in range(canopy_layers):
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=min(3, 1 + detail),
                                               radius=1.0 - i * 0.22,
                                               location=(SPAWN_X, 0, 2.3 + i * 0.5))
        leaf = bpy.context.active_object
        leaf.select_set(True)
    trunk.select_set(True)
    bpy.context.view_layer.objects.active = trunk
    bpy.ops.object.join()
    disp = trunk.modifiers.new("Rough", "DISPLACE")
    disp.texture = _disp_texture(name + "_leaf", "CLOUDS", 0.3)
    disp.strength = 0.15 + detail * 0.05
    _shade_smooth(trunk)
    return trunk
''',

"building_kit": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    bpy.ops.mesh.primitive_cube_add(scale=(2.0, 2.0, 3.0), location=(SPAWN_X, 0, 3))
    obj = bpy.context.active_object
    obj.name = name
    bev = obj.modifiers.new("Bevel", "BEVEL")
    bev.width = 0.05
    bev.segments = 2
    # simple roof
    bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=3.0, radius2=0.0, depth=2.0,
                                    location=(SPAWN_X, 0, 7.0), rotation=(0, 0, 0.785))
    roof = bpy.context.active_object
    roof.select_set(True)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    return obj
''',

"scatter_kit": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    import math
    bpy.ops.mesh.primitive_plane_add(size=8.0, location=(SPAWN_X, 0, 0))
    ground = bpy.context.active_object
    ground.name = name
    n = max(4, detail * 2)
    for i in range(n):
        a = (i / n) * math.tau
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.4,
                                               location=(SPAWN_X + math.cos(a) * 3.0,
                                                         math.sin(a) * 3.0, 0.4))
        rock = bpy.context.active_object
        d = rock.modifiers.new("D", "DISPLACE")
        d.texture = _disp_texture(name + "_s", "VORONOI", 0.6)
        d.strength = 0.3
        rock.select_set(True)
    ground.select_set(True)
    bpy.context.view_layer.objects.active = ground
    bpy.ops.object.join()
    return ground
''',

"prop_kit": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    _spawn(name)
    bpy.ops.mesh.primitive_cube_add(size=1.5, location=(SPAWN_X, 0, 1))
    obj = bpy.context.active_object
    obj.name = name
    bev = obj.modifiers.new("Bevel", "BEVEL")
    bev.width = 0.08
    bev.segments = max(1, detail)
    sub = obj.modifiers.new("Subsurf", "SUBSURF")
    sub.levels = sub.render_levels = 1 if detail < 4 else 2
    _shade_smooth(obj)
    return obj
''',

"humanoid_base": _PRELUDE + '''
def _build_geometry(name, color_hex, detail, seed=0):
    # Procedural humanoids are out of scope; build a capsule-ish biped placeholder
    # (torso + head) so the rig/anim/export steps have plausible proportions. Real
    # characters come from the diffusion route or an imported mesh.
    _spawn(name)
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.45, depth=1.6,
                                        location=(SPAWN_X, 0, 1.0))
    body = bpy.context.active_object
    body.name = name
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.32,
                                         location=(SPAWN_X, 0, 2.1))
    head = bpy.context.active_object
    head.select_set(True)
    body.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    sub = body.modifiers.new("Subsurf", "SUBSURF")
    sub.levels = sub.render_levels = 1
    _shade_smooth(body)
    return body
''',
}
