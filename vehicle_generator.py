#
# vehicle_generator.py
#
# A Blender script that procedurally generates *textured* 3D vehicle models,
# adapted from Michael Davies' SpaceshipGenerator:
#   https://github.com/a1studmuffin/SpaceshipGenerator   (MIT)
#
# What we kept from the original pattern:
#   * Build ONE hull mesh with bmesh by extruding/scaling faces (no booleans).
#   * Re-usable face toolkit (extrude_face / scale_face / get_face_matrix / ...).
#   * "Greeble" detail functions that subdivide a face and push cells in/out.
#   * Box-projected textures via the Object texture-coordinate node, so we never
#     have to unwrap UVs — and a normal map for surface micro-detail.
#   * Emissive materials for the glowy bits (there: engine burn / windows;
#     here: headlights / taillights).
#
# What changed for *vehicles* (vs. spaceships):
#   * The build is DIRECTED, not random-extruded. A car has a canonical
#     silhouette (lower body -> inset greenhouse -> raked glass -> bumpers), so
#     we track each face we create (roof / glass walls / front / rear) instead
#     of categorising afterwards by normal+dice-roll. Variation comes from the
#     parameter dict + a seed, not from random topology.
#   * Wheels are real wheels: capped cylinders with an axle along X, a chrome
#     hub, and a rubber tyre material, sunk into the body corners.
#   * Material set is a car set: metallic paint (with clearcoat), dark glass,
#     black trim, chrome, emissive head/taillights, matte rubber.
#   * The procedural normal map is generated from shader NODES (noise -> bump),
#     so the script is fully self-contained — no .png assets to ship, unlike the
#     original which loaded hull_normal.png etc.
#
# Conventions match this repo (src/blender/base_models.py): Z-up, +Y is forward,
# X is width. The finished object is dropped on the ground (lowest point at z=0).
#
# Blender 2.8 - 4.x. The Principled-BSDF socket names changed in 4.0
# ("Specular" -> "Specular IOR Level", "Emission" -> "Emission Color", etc.) and
# bmesh.ops.create_cone's diameter args were renamed to radius in 3.0 — both are
# handled with tolerant wrappers so the same file runs everywhere.
#

import os
import json
from math import atan2, pi, radians
from random import random, seed, uniform, randint
from colorsys import hls_to_rgb
from enum import IntEnum

# Blender-only modules. Wrapped so this file can be imported OUTSIDE Blender
# (procedural_pipeline.py reuses PARAM_SCHEMA / clamp_params / keyword_vehicle_params
# / build_detailed_code for prompt mapping + codegen). The geometry functions that
# actually need these only run inside Blender, so a None here is harmless until then.
try:
    import bpy
    import bmesh
    from mathutils import Vector, Matrix
except ImportError:  # not running inside Blender
    bpy = bmesh = Vector = Matrix = None


# ──────────────────────────────────────────────────────────────────────────────
# Version-tolerant API wrappers (so one file runs on Blender 2.8 - 4.x)
# ──────────────────────────────────────────────────────────────────────────────

def _set_input(node, candidate_names, value):
    """Set the first Principled-BSDF input that exists from a list of candidate
    socket names. Socket names were reshuffled in Blender 4.0, so we try the new
    name first, then fall back. Returns True if one was set."""
    for name in candidate_names:
        sock = node.inputs.get(name)
        if sock is not None:
            sock.default_value = value
            return True
    return False


def _set_emission(node, rgba, strength):
    """Set emission across versions. 4.x has separate Emission Color + Strength;
    pre-4.0 only has an 'Emission' colour, so bake the strength into the colour."""
    if _set_input(node, ["Emission Color"], rgba):
        _set_input(node, ["Emission Strength"], strength)
    else:
        boosted = tuple(min(1.0, c * strength) for c in rgba[:3]) + (1.0,)
        _set_input(node, ["Emission"], boosted)


def _cone(bm, **kw):
    """bmesh.ops.create_cone with radius1/radius2 (Blender 3.0+), falling back to
    the old diameter1/diameter2 names on older builds."""
    try:
        return bmesh.ops.create_cone(bm, **kw)
    except TypeError:
        kw = dict(kw)
        if "radius1" in kw:
            kw["diameter1"] = kw.pop("radius1")
        if "radius2" in kw:
            kw["diameter2"] = kw.pop("radius2")
        return bmesh.ops.create_cone(bm, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# Face toolkit (lifted from SpaceshipGenerator, lightly trimmed)
# ──────────────────────────────────────────────────────────────────────────────

def extrude_face(bm, face, translate_forwards=0.0, extruded_face_list=None):
    """Extrude a face along its normal. Returns the new (cap) face; if
    extruded_face_list is given, the new side faces are appended to it."""
    new_faces = bmesh.ops.extrude_discrete_faces(bm, faces=[face])["faces"]
    if extruded_face_list is not None:
        extruded_face_list += new_faces[:]
    new_face = new_faces[0]
    bmesh.ops.translate(bm, vec=new_face.normal * translate_forwards, verts=new_face.verts)
    return new_face


def get_face_matrix(face, pos=None):
    """Rough 4x4 transform for a face (ignores shear), optionally re-positioned."""
    x_axis = (face.verts[1].co - face.verts[0].co).normalized()
    z_axis = -face.normal
    y_axis = z_axis.cross(x_axis)
    if not pos:
        pos = face.calc_center_bounds()
    mat = Matrix()
    for i, axis in enumerate((x_axis, y_axis, z_axis)):
        mat[0][i], mat[1][i], mat[2][i], mat[3][i] = axis.x, axis.y, axis.z, 0
    mat[0][3], mat[1][3], mat[2][3], mat[3][3] = pos.x, pos.y, pos.z, 1
    return mat


def scale_face(bm, face, scale_x, scale_y, scale_z):
    """Scale a face in its own local space (so an inset stays planar)."""
    face_space = get_face_matrix(face)
    face_space.invert()
    bmesh.ops.scale(bm, vec=Vector((scale_x, scale_y, scale_z)),
                    space=face_space, verts=face.verts)


def get_face_width_and_height(face):
    if not face.is_valid or len(face.verts[:]) < 4:
        return -1, -1
    width = (face.verts[0].co - face.verts[1].co).length
    height = (face.verts[2].co - face.verts[1].co).length
    return width, height


def get_aspect_ratio(face):
    if not face.is_valid:
        return 1.0
    ar = max(0.01, face.edges[0].calc_length() / face.edges[1].calc_length())
    return ar if ar >= 1.0 else 1.0 / ar


def face_by_normal(bm, axis):
    """Return the face whose normal best matches a unit axis Vector."""
    return max(bm.faces, key=lambda f: f.normal.dot(axis))


# ──────────────────────────────────────────────────────────────────────────────
# Materials
# ──────────────────────────────────────────────────────────────────────────────

class Material(IntEnum):
    paint = 0        # metallic body paint (box-projected noise normal + clearcoat)
    glass = 1        # dark tinted greenhouse glass
    trim = 2         # black plastic / grille / bumper rubber
    chrome = 3       # bright metal (wheel hubs, exhaust)
    headlight = 4    # emissive white
    taillight = 5    # emissive red
    tire = 6         # matte black rubber


def _principled(mat):
    return mat.node_tree.nodes.get("Principled BSDF")


def _add_box_projected_noise_normal(mat, scale=18.0, strength=0.12):
    """Drive the Principled 'Normal' from a procedural noise bump, projected in
    OBJECT space — the box-projection trick from the original (no UVs needed).
    Gives paint a subtle panel/orange-peel micro-surface so it reads as textured
    under light rather than dead flat."""
    nt = mat.node_tree
    bsdf = _principled(mat)
    if bsdf is None:
        return
    coord = nt.nodes.new("ShaderNodeTexCoord")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    if "Detail" in noise.inputs:
        noise.inputs["Detail"].default_value = 6.0
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    nt.links.new(coord.outputs["Object"], noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def create_materials(params=None):
    """Create the full car material set and return it as a list indexed by the
    Material enum (mirrors the original's create_materials()). When `params` carries
    a colour / finish, the body paint honours it; otherwise a random hue is used."""
    params = params or {}
    mats = []
    for m in Material:
        mat = bpy.data.materials.new(name="veh_" + m.name)
        mat.use_nodes = True
        mats.append(mat)

    # Body paint colour: explicit override, else a believable random hue.
    col = params.get("color")
    if col:
        paint_rgba = (col[0], col[1], col[2], 1.0)
    else:
        rgb = hls_to_rgb(random(), uniform(0.30, 0.55), uniform(0.55, 0.95))
        paint_rgba = (rgb[0], rgb[1], rgb[2], 1.0)

    # paint — finish from params (metallic/roughness/clearcoat) + procedural micro-normal
    b = _principled(mats[Material.paint])
    _set_input(b, ["Base Color"], paint_rgba)
    _set_input(b, ["Metallic"], params.get("metallic", 0.65))
    _set_input(b, ["Roughness"], params.get("roughness", 0.30))
    _set_input(b, ["Coat Weight", "Clearcoat"], params.get("clearcoat", 1.0))
    _add_box_projected_noise_normal(mats[Material.paint])

    # glass — dark, smooth, a little see-through
    b = _principled(mats[Material.glass])
    _set_input(b, ["Base Color"], (0.02, 0.03, 0.04, 1.0))
    _set_input(b, ["Roughness"], 0.05)
    _set_input(b, ["Metallic"], 0.0)
    _set_input(b, ["Transmission Weight", "Transmission"], 0.4)

    # trim — matte black plastic / bumpers / grille
    b = _principled(mats[Material.trim])
    _set_input(b, ["Base Color"], (0.015, 0.015, 0.015, 1.0))
    _set_input(b, ["Roughness"], 0.7)

    # chrome — bright polished metal
    b = _principled(mats[Material.chrome])
    _set_input(b, ["Base Color"], (0.8, 0.8, 0.82, 1.0))
    _set_input(b, ["Metallic"], 1.0)
    _set_input(b, ["Roughness"], 0.12)

    # headlight — emissive white
    _set_emission(_principled(mats[Material.headlight]), (1.0, 0.97, 0.9, 1.0), 6.0)

    # taillight — emissive red
    _set_emission(_principled(mats[Material.taillight]), (1.0, 0.05, 0.03, 1.0), 5.0)

    # tire — matte black rubber
    b = _principled(mats[Material.tire])
    _set_input(b, ["Base Color"], (0.012, 0.012, 0.012, 1.0))
    _set_input(b, ["Roughness"], 0.9)
    _set_input(b, ["Metallic"], 0.0)

    return mats


# ──────────────────────────────────────────────────────────────────────────────
# Greeble / detail (adapted from add_grid_to_face, add_disc_to_face, etc.)
# ──────────────────────────────────────────────────────────────────────────────

def recessed_grid(bm, face, mat_index, cuts=3, depth=0.04, scale=0.82):
    """Subdivide a face into a grid and push each cell inward — a recessed panel.
    Used for the front grille and side vents (the inverse of the original's
    add_grid_to_face, which extruded cells outward)."""
    if not face.is_valid:
        return
    res = bmesh.ops.subdivide_edges(bm, edges=face.edges[:], cuts=cuts,
                                    use_grid_fill=True)
    for f in res["geom"]:
        if isinstance(f, bmesh.types.BMFace):
            f.material_index = mat_index
            inner = extrude_face(bm, f, -depth)
            scale_face(bm, inner, scale, scale, scale)
            inner.material_index = mat_index


def add_disc(bm, pos, axis_char, radius, depth, mat_index, segments=20):
    """A domed lens facing along an axis — head/taillights, exhaust tips, beacons
    — not a flat disc: the far cap tapers to 78% radius so the shape actually
    reads as a lens/bezel instead of a coin. axis_char is 'X', 'Y' or 'Z' (the
    direction the dome faces; a cone's native axis is already Z)."""
    if axis_char == "Y":
        rot = Matrix.Rotation(radians(90), 4, "X")
    elif axis_char == "X":
        rot = Matrix.Rotation(radians(90), 4, "Y")
    else:  # 'Z' — native orientation
        rot = Matrix.Identity(4)
    mat = Matrix.Translation(Vector(pos)) @ rot
    res = _cone(bm, cap_ends=True, cap_tris=False, segments=segments,
                radius1=radius, radius2=radius * 0.78, depth=depth, matrix=mat)
    for v in res["verts"]:
        for f in v.link_faces:
            f.material_index = mat_index


def add_wheel(bm, pos, wheel_r, wheel_w, rim, segments=24):
    """A wheel as part of the hull bmesh: a treaded tyre (alternating recessed
    grooves around the tread band, real geometry not a plain cylinder) + a
    spoked chrome hub, axle along X. A ~40 degree arc at the very bottom is kept
    at full radius (never recessed) so the wheel still sits flush on the ground
    regardless of which tread face a given segment count lands a groove on —
    _finalize()'s whole-vehicle ground bake and rig_vehicle()'s `wheel_z = wheel_r`
    axle-height assumption both depend on the tyre's true lowest point staying at
    exactly wheel_r below the axle."""
    pos = Vector(pos)
    mat = Matrix.Translation(pos) @ Matrix.Rotation(radians(90), 4, "Y")
    res = _cone(bm, cap_ends=True, cap_tris=False, segments=segments,
                radius1=wheel_r, radius2=wheel_r, depth=wheel_w, matrix=mat)
    tyre_verts = set(res["verts"])
    side_faces = []
    seen = set()
    for v in res["verts"]:
        for f in v.link_faces:
            if f in seen or not all(vv in tyre_verts for vv in f.verts):
                continue
            seen.add(f)
            local_x = (f.calc_center_median() - pos).x
            if abs(local_x) < wheel_w * 0.45:      # side band, not the end caps
                side_faces.append(f)

    def _angle(f):
        c = f.calc_center_median() - pos
        return atan2(c.z, c.y)                      # axle along X, cross-section YZ

    side_faces.sort(key=_angle)
    groove_depth = wheel_r * 0.03
    for i, f in enumerate(side_faces):
        if not f.is_valid:
            continue
        f.material_index = Material.tire
        near_bottom = abs(_angle(f) - (-1.5708)) < 0.35   # contact-patch arc, stays full radius
        if i % 2 == 1 and not near_bottom:
            try:
                inner = extrude_face(bm, f, -groove_depth)
                for iv in inner.verts:
                    for ff in iv.link_faces:
                        ff.material_index = Material.tire
            except Exception:
                pass
    if rim > 0.05:
        hub = _cone(bm, cap_ends=True, cap_tris=False, segments=12,
                    radius1=wheel_r * rim, radius2=wheel_r * rim,
                    depth=wheel_w * 1.06, matrix=mat)
        hub_verts = set(hub["verts"])
        end_faces, seen2 = [], set()
        for v in hub["verts"]:
            for f in v.link_faces:
                if f in seen2 or not all(vv in hub_verts for vv in f.verts):
                    continue
                seen2.add(f)
                f.material_index = Material.chrome
                if abs((f.calc_center_median() - pos).x) > wheel_w * 0.45:
                    end_faces.append(f)             # the two flat hub caps
        for f in end_faces:
            try:
                recessed_grid(bm, f, Material.chrome, cuts=2, depth=wheel_r * 0.03, scale=0.7)
            except Exception:
                pass


def add_slab(bm, center, size, mat_index, taper=0.32):
    """A wing/stabiliser/fin/prop-blade slab, tapered from a full-section leading
    edge to a thin trailing edge (an aerofoil silhouette) instead of a flat box.
    `size` is (X, Y, Z) full extents; on every existing call site in this file, Y
    is the chord (wing/stabiliser/fin) or the thin flat dimension (prop blades),
    so tapering the +Y face toward the -Y face reads correctly for all of them."""
    mat = Matrix.Translation(Vector(center)) @ Matrix.Diagonal(Vector(size)).to_4x4()
    res = bmesh.ops.create_cube(bm, size=1.0, matrix=mat)
    new_verts = set(res["verts"])
    new_faces = set()
    for v in res["verts"]:
        for f in v.link_faces:
            if all(vv in new_verts for vv in f.verts):
                new_faces.add(f)
    trailing = max(new_faces, key=lambda f: f.calc_center_median().y)
    scale_face(bm, trailing, taper, taper, 1.0)
    for f in new_faces:
        f.material_index = mat_index
    return res


def add_strut(bm, top, bot, r, mat_index, segments=8):
    """A landing-gear leg: two tapered segments meeting at a joint bulge midway
    (not a plain uniform cylinder)."""
    top, bot = Vector(top), Vector(bot)
    d = top - bot
    length = max(0.02, d.length)
    axis = d.normalized()
    up = Vector((0, 0, 1)) if abs(axis.z) < 0.99 else Vector((1, 0, 0))
    x_axis = axis.cross(up).normalized()
    y_axis = axis.cross(x_axis)
    rot = Matrix((x_axis, y_axis, axis)).transposed().to_4x4()

    def _seg(a, b, r0, r1):
        seg_len = (b - a).length
        if seg_len < 1e-6:
            return
        mat = Matrix.Translation((a + b) * 0.5) @ rot
        res = _cone(bm, cap_ends=True, cap_tris=False, segments=segments,
                    radius1=r0, radius2=r1, depth=seg_len, matrix=mat)
        for v in res["verts"]:
            for f in v.link_faces:
                f.material_index = mat_index

    mid = bot + axis * (length * 0.5)
    joint_r = r * 1.35
    _seg(bot, mid, r, joint_r)
    _seg(mid, top, joint_r, r)


# ──────────────────────────────────────────────────────────────────────────────
# Parameters (the LLM-tunable knobs, same spirit as base_models.BASE_PARAM_SCHEMA)
# ──────────────────────────────────────────────────────────────────────────────

# {param: (default, lo, hi)} — the body silhouette + stance.
PARAM_SCHEMA = {
    "body_w":     (1.85, 1.40, 2.30),
    "body_len":   (4.30, 3.20, 5.60),
    "body_h":     (0.78, 0.55, 1.30),   # taller -> SUV/truck
    "cabin_h":    (0.62, 0.35, 1.00),
    "cabin_len":  (0.52, 0.34, 0.78),   # greenhouse length as a fraction of body
    "cabin_pos":  (0.02, -0.25, 0.30),  # greenhouse shift: +front / -rear
    "rake":       (0.22, 0.05, 0.45),   # windshield/backlight slope (0=upright box)
    "wheel_r":    (0.46, 0.32, 0.95),
    "wheel_w":    (0.34, 0.22, 0.62),
    "rim":        (0.55, 0.0, 0.72),    # hub radius as a fraction of the tyre
    "bumper":     (0.18, 0.0, 0.40),    # front/rear overhang length
    # finish knobs (drive the paint material; "matte" => metallic 0 + high roughness)
    "metallic":   (0.65, 0.0, 1.0),
    "roughness":  (0.30, 0.05, 1.0),
    "clearcoat":  (1.0, 0.0, 1.0),
}

# colour keyword -> rgb (0..1), used by the deterministic fallback mapper.
_COLORS = {
    "black": (0.02, 0.02, 0.02), "white": (0.90, 0.90, 0.90),
    "red": (0.60, 0.04, 0.04), "blue": (0.05, 0.12, 0.50),
    "green": (0.05, 0.35, 0.10), "yellow": (0.80, 0.70, 0.05),
    "orange": (0.85, 0.35, 0.03), "silver": (0.60, 0.62, 0.65),
    "grey": (0.30, 0.30, 0.32), "gray": (0.30, 0.30, 0.32),
    "purple": (0.30, 0.05, 0.50), "brown": (0.25, 0.13, 0.05),
}


def _clamp_color(v):
    """Validate/normalise a colour. Returns None when absent so create_materials()
    falls back to a random hue (matches base_models' tolerant approach)."""
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            c = [float(x) for x in v[:3]]
        except (TypeError, ValueError):
            return None
        if max(c) > 1.0:                      # LLM sometimes emits 0-255
            c = [x / 255.0 for x in c]
        return tuple(max(0.0, min(1.0, x)) for x in c)
    return None


# {param: (default, lo, hi)} for the detailed aircraft.
PLANE_PARAM_SCHEMA = {
    "fus_len":    (5.0, 3.0, 9.0),      # fuselage length (along +Y, nose forward)
    "fus_w":      (0.95, 0.55, 1.60),   # fuselage width
    "fus_h":      (1.0, 0.6, 1.8),      # fuselage height
    "wing_span":  (6.5, 3.0, 12.0),
    "wing_chord": (1.4, 0.6, 2.6),
    "wing_pos":   (0.05, -0.40, 0.40),  # wing fore(+)/aft(-) along the fuselage
    "fin_h":      (1.2, 0.5, 2.4),      # vertical tail height
    "canopy":     (1.0, 0.0, 1.0),      # >=0.5 -> glass cockpit canopy
    "jets":       (0.0, 0.0, 1.0),      # >=0.5 -> wing jet nacelles; else nose propeller
    "gear":       (1.0, 0.0, 1.0),      # >=0.5 -> tricycle landing gear
    # finish knobs (shared meaning with the car schema)
    "metallic":   (0.55, 0.0, 1.0),
    "roughness":  (0.35, 0.05, 1.0),
    "clearcoat":  (0.6, 0.0, 1.0),
}


def _clamp_to_schema(raw, schema):
    """Fill defaults + clamp every numeric knob; normalise colour (None = random).
    A bad token can never break the builder."""
    raw = raw or {}
    out = {}
    for name, (default, lo, hi) in schema.items():
        try:
            out[name] = max(lo, min(hi, float(raw.get(name, default))))
        except (TypeError, ValueError):
            out[name] = default
    out["color"] = _clamp_color(raw.get("color"))
    return out


def clamp_params(raw):
    """Clamp car params (kept as the original name for back-compat)."""
    return _clamp_to_schema(raw, PARAM_SCHEMA)


def clamp_plane_params(raw):
    """Clamp aircraft params."""
    return _clamp_to_schema(raw, PLANE_PARAM_SCHEMA)


def keyword_plane_params(prompt):
    """Deterministic prompt -> raw aircraft param dict (no LLM)."""
    p = prompt.lower()
    out = {}
    if "jet" in p or "fighter" in p:
        out.update(jets=1.0, fus_w=0.80, wing_span=7.0, wing_chord=1.7, fin_h=1.6)
    if any(w in p for w in ("jumbo", "airliner", "passenger", "big", "large")):
        out.update(jets=1.0, fus_len=8.0, fus_w=1.45, wing_span=10.5, wing_chord=2.2)
    if "glider" in p or "sailplane" in p:
        out.update(jets=0.0, gear=0.0, wing_span=11.0, wing_chord=0.9, fus_w=0.6)
    if any(w in p for w in ("small", "light", "cessna", "prop", "propeller")):
        out.update(jets=0.0, fus_len=3.6, wing_span=5.0)
    if "biplane" in p:
        out.update(jets=0.0, fus_len=4.0, wing_span=6.5, wing_chord=1.3)
    for word, rgb in _COLORS.items():
        if word in p:
            out["color"] = rgb
            break
    return out


def keyword_vehicle_params(prompt):
    """Deterministic prompt -> raw param dict (no LLM). The baseline mapper; the
    pipeline clamps the result. Conservative — only unmistakable cues."""
    p = prompt.lower()
    out = {}
    # vehicle class
    if "pickup" in p or "truck" in p:
        out.update(body_h=1.0, body_len=5.2, cabin_len=0.40, cabin_pos=0.18, wheel_r=0.62)
    if "suv" in p or "jeep" in p:
        out.update(body_h=1.05, cabin_h=0.82, wheel_r=0.60, body_len=4.6)
    if "sports" in p or "supercar" in p or "coupe" in p:
        out.update(body_h=0.58, cabin_h=0.40, rake=0.42, wheel_r=0.40, wheel_w=0.50)
    if "van" in p:
        out.update(body_h=1.15, cabin_len=0.70, body_len=4.8)
    if "limo" in p or "long" in p:
        out["body_len"] = 5.5
    # stance
    if any(w in p for w in ("lifted", "monster", "off-road", "offroad", "raised")):
        out["wheel_r"] = max(out.get("wheel_r", 0.46), 0.80)
        out["body_h"] = max(out.get("body_h", 0.78), 1.00)
        out["wheel_w"] = 0.55
    if "lowered" in p or "slammed" in p:
        out["wheel_r"] = 0.36
    if "wide" in p:
        out["body_w"] = 2.2
    # finish
    if "matte" in p or "flat black" in p:
        out.update(metallic=0.0, roughness=0.85, clearcoat=0.0)
    if "chrome" in p or "metallic" in p or "glossy" in p:
        out.update(metallic=1.0, roughness=0.12, clearcoat=1.0)
    # colour (first keyword wins)
    for word, rgb in _COLORS.items():
        if word in p:
            out["color"] = rgb
            break
    return out


# ──────────────────────────────────────────────────────────────────────────────
# The generator
# ──────────────────────────────────────────────────────────────────────────────

def generate_vehicle(random_seed="", params=None, name="Vehicle",
                     assign_materials=True, apply_bevel=True):
    """Build one textured vehicle mesh and return the object. Pass a random_seed
    string for a repeatable result, and/or a params dict (see PARAM_SCHEMA)."""
    if random_seed:
        seed(random_seed)
    p = clamp_params(params or {})

    bw, bl, bh = p["body_w"], p["body_len"], p["body_h"]
    ch, clen, cpos = p["cabin_h"], p["cabin_len"], p["cabin_pos"]
    rake, wr, ww, rim, bump = p["rake"], p["wheel_r"], p["wheel_w"], p["rim"], p["bumper"]

    bm = bmesh.new()
    # Lower body: a cube scaled to car proportions, centred at the origin.
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector((bw, bl, bh)), verts=bm.verts)

    # ── Greenhouse: inset the top face, then raise it, then rake the glass ──
    top = face_by_normal(bm, Vector((0, 0, 1)))
    deck = extrude_face(bm, top, 0.0)                 # duplicate top -> creates deck ring
    scale_face(bm, deck, 0.84, clen / 0.9, 1.0)       # inset (leaves hood + trunk deck)
    bmesh.ops.translate(bm, vec=Vector((0, cpos * bl, 0)), verts=deck.verts)

    walls = []
    roof = extrude_face(bm, deck, ch, walls)          # raise greenhouse, capture walls
    # Rake the glass: pull the top of the greenhouse inward along Y so the
    # windshield and backlight slope instead of standing vertical.
    cy = sum(v.co.y for v in roof.verts) / len(roof.verts)
    for v in roof.verts:
        v.co.y += -rake if v.co.y > cy else rake
    # Greenhouse walls -> glass; the roof cap stays paint.
    for w in walls:
        w.material_index = Material.glass
    roof.material_index = Material.paint

    # ── Front + rear: extrude short bumpers, then detail the end faces ──
    front = face_by_normal(bm, Vector((0, 1, 0)))
    rear = face_by_normal(bm, Vector((0, -1, 0)))
    if bump > 0.01:
        front = extrude_face(bm, front, bump)
        rear = extrude_face(bm, rear, bump)

    # Grille recessed into the lower front, headlights at the upper corners.
    fw, fh = get_face_width_and_height(front)
    front_y = max(v.co.y for v in front.verts)
    try:
        recessed_grid(bm, front, Material.trim, cuts=randint(2, 4),
                      depth=0.05, scale=0.8)
    except Exception:
        pass
    hz = bh * 0.18
    hl_r = min(0.16, bw * 0.16)
    for sx in (1, -1):
        add_disc(bm, Vector((sx * bw * 0.32, front_y + 0.01, hz)),
                 "Y", hl_r, 0.05, Material.headlight)

    # Taillights across the rear, twin exhaust tips below.
    rear_y = min(v.co.y for v in rear.verts)
    for sx in (1, -1):
        add_disc(bm, Vector((sx * bw * 0.33, rear_y - 0.01, hz)),
                 "Y", min(0.13, bw * 0.14), 0.04, Material.taillight)
    for sx in (1, -1):
        add_disc(bm, Vector((sx * bw * 0.28, rear_y - 0.04, -bh * 0.42)),
                 "Y", 0.05, 0.10, Material.chrome)

    # Side vents/character line on the lower doors (subtle greeble like the ship).
    for axis in (Vector((1, 0, 0)), Vector((-1, 0, 0))):
        side = face_by_normal(bm, axis)
        if random() > 0.5:
            try:
                recessed_grid(bm, side, Material.trim, cuts=randint(1, 2),
                              depth=0.03, scale=0.9)
            except Exception:
                pass

    # ── Wheels: four, sunk into the lower-body corners, axle along X ──
    wheel_z = -bh * 0.5 + wr * 0.18
    wheel_y = bl * 0.5 - wr - 0.10
    wheel_x = bw * 0.5 + ww * 0.5 - 0.06
    for sy in (1, -1):
        for sx in (1, -1):
            add_wheel(bm, Vector((sx * wheel_x, sy * wheel_y, wheel_z)), wr, ww, rim)

    return _finalize(bm, p, name, assign_materials, apply_bevel)


def _finalize(bm, params, name, assign_materials, apply_bevel, smooth_angle=40.0):
    """Shared tail for the detailed builders: write the bmesh to a mesh, assign the
    material set, drop the object on the ground (lowest point -> z=0), bevel the
    edges and smooth-shade. Returns the new object."""
    if not assign_materials:
        for f in bm.faces:
            f.material_index = 0

    # Sharp creases are baked into the edge data itself: Blender 4.1 removed
    # mesh.use_auto_smooth, so an angle threshold applied at shading time no
    # longer exists there — edges above the angle must carry smooth=False for
    # every supported version to split normals identically.
    bm.normal_update()
    sharp = radians(smooth_angle)
    for e in bm.edges:
        if e.calc_face_angle(0.0) > sharp:
            e.smooth = False

    me = bpy.data.meshes.new(name + "_mesh")
    bm.to_mesh(me)
    bm.free()

    if assign_materials:
        for mat in create_materials(params):
            me.materials.append(mat)

    obj = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    lowest = min((obj.matrix_world @ v.co).z for v in me.vertices)
    obj.location.z -= lowest

    if apply_bevel:
        bev = obj.modifiers.new("Bevel", "BEVEL")
        bev.width = 0.02
        bev.segments = 2
        bev.limit_method = "ANGLE"
        bev.angle_limit = radians(50)

    for poly in me.polygons:
        poly.use_smooth = True
    if hasattr(me, "use_auto_smooth"):  # pre-4.1: flag must be on for edge splits;
        me.use_auto_smooth = True       # angle=pi defers entirely to the explicit
        me.auto_smooth_angle = pi       # smooth=False edges marked above


    return obj


# ──────────────────────────────────────────────────────────────────────────────
# Detailed aircraft (same SpaceshipGenerator-style toolkit, plane silhouette).
# +Y is forward (nose), Z up — consistent with generate_vehicle.
# ──────────────────────────────────────────────────────────────────────────────

def generate_plane(random_seed="", params=None, name="Plane",
                   assign_materials=True, apply_bevel=True):
    """Build one textured aircraft mesh and return the object."""
    if random_seed:
        seed(random_seed)
    p = clamp_plane_params(params or {})
    fl, fw, fh = p["fus_len"], p["fus_w"], p["fus_h"]
    span, chord, wpos = p["wing_span"], p["wing_chord"], p["wing_pos"]
    fin_h = p["fin_h"]
    canopy = p["canopy"] >= 0.5
    jets = p["jets"] >= 0.5
    gear = p["gear"] >= 0.5

    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector((fw, fl, fh)), verts=bm.verts)

    # Nose: extrude forward (+Y) and taper to a point.
    front = face_by_normal(bm, Vector((0, 1, 0)))
    front = extrude_face(bm, front, fl * 0.18)
    scale_face(bm, front, 0.32, 1.0, 0.32)
    # Tail: slight boat-tail taper.
    rear = face_by_normal(bm, Vector((0, -1, 0)))
    rear = extrude_face(bm, rear, fl * 0.04)
    scale_face(bm, rear, 0.5, 1.0, 0.6)

    # Glass canopy: inset+raise a patch toward the front of the spine.
    if canopy:
        top = face_by_normal(bm, Vector((0, 0, 1)))
        cp = extrude_face(bm, top, 0.0)
        scale_face(bm, cp, 0.66, 0.30, 1.0)
        bmesh.ops.translate(bm, vec=Vector((0, fl * 0.16, 0)), verts=cp.verts)
        walls = []
        cp = extrude_face(bm, cp, fh * 0.35, walls)
        for w in walls:
            w.material_index = Material.glass
        cp.material_index = Material.glass

    nose_y = max(v.co.y for v in bm.verts)
    tail_y = -fl * 0.5 + chord * 0.2
    wing_y = wpos * fl * 0.3

    # Main wings, horizontal stabiliser, vertical fin.
    add_slab(bm, (0, wing_y, -fh * 0.08), (span, chord, fh * 0.12), Material.paint)
    add_slab(bm, (0, tail_y, 0), (span * 0.42, chord * 0.55, fh * 0.10), Material.paint)
    add_slab(bm, (0, tail_y, fh * 0.5 + fin_h * 0.5), (fh * 0.12, chord * 0.6, fin_h), Material.paint)

    # Propulsion: nose propeller (default) or under-wing jet nacelles.
    if jets:
        for sx in (1, -1):
            mat = Matrix.Translation(Vector((sx * span * 0.27, wing_y, -fh * 0.32))) @ \
                Matrix.Rotation(radians(90), 4, "X")
            res = _cone(bm, cap_ends=True, cap_tris=False, segments=16,
                        radius1=fw * 0.30, radius2=fw * 0.26, depth=chord * 1.3, matrix=mat)
            for v in res["verts"]:
                for f in v.link_faces:
                    f.material_index = Material.trim
    else:
        add_disc(bm, Vector((0, nose_y + 0.06, 0)), "Y", fw * 0.18, 0.12, Material.chrome)
        blade = fh * 1.7
        add_slab(bm, (0, nose_y + 0.07, 0), (fw * 0.12, 0.04, blade), Material.trim)
        add_slab(bm, (0, nose_y + 0.07, 0), (blade, 0.04, fw * 0.12), Material.trim)

    # Nav lights: white landing light at the nose, red beacon atop the fin.
    add_disc(bm, Vector((0, nose_y - 0.02, -fh * 0.18)), "Y", fw * 0.12, 0.04, Material.headlight)
    add_disc(bm, Vector((0, tail_y, fh * 0.5 + fin_h + 0.03)), "Z", fw * 0.10, 0.05, Material.taillight)

    # Tricycle landing gear: nose leg + two main legs under the wings.
    if gear:
        gr = fh * 0.16
        gw = fw * 0.16
        leg_top = -fh * 0.5
        leg_bot = -fh - gr * 0.2
        for gx, gy in ((0.0, nose_y - fl * 0.22), (span * 0.14, wing_y), (-span * 0.14, wing_y)):
            add_strut(bm, (gx, gy, leg_top), (gx, gy, leg_bot + gr), fw * 0.06, Material.trim)
            add_wheel(bm, Vector((gx, gy, leg_bot)), gr, gw, 0.4)

    return _finalize(bm, p, name, assign_materials, apply_bevel)


def rig_plane(obj, params=None, name=None, demo=False):
    """Armature for the detailed plane: root + body + a propeller bone that spins
    (prop planes only). Prop verts are found by material (trim blades + the chrome
    spinner near the nose); everything else binds to the body. Returns the rig."""
    if bpy is None:
        raise RuntimeError("rig_plane requires Blender")
    p = clamp_plane_params(params or {})
    name = name or obj.name
    fl, fw, fh = p["fus_len"], p["fus_w"], p["fus_h"]
    has_prop = p["jets"] < 0.5

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    me = obj.data
    nose_y = max(v.co.y for v in me.vertices)
    spinner_z = fh * 0.5                                  # rough fuselage-centre height after the drop
    nose = Vector((0, nose_y, spinner_z))

    arm = bpy.data.armatures.new(name + "_arm")
    rig = bpy.data.objects.new(name + "_rig", arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones
    root = eb.new("root"); root.head = (0, 0, 0.02); root.tail = (0, 0, 0.4)
    body = eb.new("body"); body.head = (0, 0, spinner_z); body.tail = (0, fl * 0.4, spinner_z); body.parent = root
    if has_prop:
        pr = eb.new("Prop")
        pr.head = nose
        pr.tail = (0, nose_y + 0.3, nose.z)              # axle along +Y (spin axis)
        pr.parent = root
    bpy.ops.object.mode_set(mode="OBJECT")

    # Prop verts: trim (blades) + chrome (spinner) polygons near the nose tip.
    trim_mi, chrome_mi = int(Material.trim), int(Material.chrome)
    prop_verts = set()
    if has_prop:
        for poly in me.polygons:
            if poly.material_index in (trim_mi, chrome_mi):
                cen = Vector(poly.center)
                if cen.y > nose_y - fl * 0.12:
                    prop_verts.update(poly.vertices)

    body_vg = obj.vertex_groups.new(name="body")
    prop_vg = obj.vertex_groups.new(name="Prop") if has_prop else None
    for v in me.vertices:
        if v.index in prop_verts:
            prop_vg.add([v.index], 1.0, "REPLACE")
        else:
            body_vg.add([v.index], 1.0, "REPLACE")

    obj.parent = rig
    m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig
    if demo and has_prop:
        import math as _m
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode="POSE")
        pb = rig.pose.bones.get("Prop")
        if pb:
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, _m.radians(45.0), 0.0)   # spin about the +Y axle
        bpy.ops.object.mode_set(mode="OBJECT")
    return rig


# ──────────────────────────────────────────────────────────────────────────────
# Rigging: make the single detailed mesh drivable.
# The detailed vehicle is ONE fused mesh, so we can't parent separate wheel
# objects to bones the way base_models does for its other bases. Instead we
# build a car skeleton (root + body + 4 wheel bones, axle along +X) and skin-bind
# the one mesh: wheel polygons are found by MATERIAL (tyre, plus chrome hubs near
# a wheel centre — which excludes the rear exhaust tips), everything else binds to
# the body bone. A wheel bone's roll then spins just that wheel's vertices.
# ──────────────────────────────────────────────────────────────────────────────

def demo_pose_vehicle(rig, spin_deg=35.0, lift=0.22):
    """Pose the rig to prove it moves: roll every wheel + lift the front axle."""
    import math as _m
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="POSE")
    for pb in rig.pose.bones:
        if pb.name.startswith("Wheel."):
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, _m.radians(spin_deg), 0.0)   # roll about axle (bone Y = +X)
            if pb.name[6:7] == "F":                                # front axle -> suspension lift
                pb.location = (0.0, 0.0, lift)
    bpy.ops.object.mode_set(mode="OBJECT")


def rig_vehicle(obj, params=None, name=None, demo=False):
    """Build a car armature and skin-bind the single detailed mesh to it so the
    wheels spin and the front axle can lift. Returns the rig object."""
    if bpy is None:
        raise RuntimeError("rig_vehicle requires Blender")
    p = clamp_params(params or {})
    name = name or obj.name
    bw, bl, bh = p["body_w"], p["body_len"], p["body_h"]
    wr, ww = p["wheel_r"], p["wheel_w"]

    # Bake the object's transform so mesh-local coords == world (tyre bottoms at
    # z=0); bones and the material-based vertex test then share one coordinate space.
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    wheel_x = bw * 0.5 + ww * 0.5 - 0.06
    wheel_y = bl * 0.5 - wr - 0.10
    wheel_z = wr                            # after baking, the axle sits at tyre radius
    body_z = bh * 0.5 + wr * 0.82           # rough body centre height (see generate_vehicle)
    centres = {}                            # bone name -> wheel-centre Vector
    for sy, ylab in ((1, "F"), (-1, "R")):
        for sx, xlab in ((1, "L"), (-1, "R")):
            centres["Wheel." + ylab + xlab] = Vector((sx * wheel_x, sy * wheel_y, wheel_z))

    arm = bpy.data.armatures.new(name + "_arm")
    rig = bpy.data.objects.new(name + "_rig", arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones
    root = eb.new("root"); root.head = (0, 0, 0.02); root.tail = (0, 0, 0.32)
    body = eb.new("body"); body.head = (0, 0, body_z); body.tail = (0, 0.6, body_z); body.parent = root
    for bn, c in centres.items():
        b = eb.new(bn); b.head = c; b.tail = (c.x + 0.3, c.y, c.z); b.parent = root  # axle +X
    bpy.ops.object.mode_set(mode="OBJECT")

    # Classify each polygon: wheel (tyre material, or a chrome hub near a wheel
    # centre) vs body, then bind that poly's verts to the nearest wheel bone / body.
    me = obj.data
    tyre_mi, chrome_mi = int(Material.tire), int(Material.chrome)
    hub_capture2 = (wr * 1.2) ** 2
    vert_bone = {}                          # vert index -> wheel bone name

    def nearest_bone(co):
        return min(centres, key=lambda bn: (co - centres[bn]).length_squared)

    for poly in me.polygons:
        cen = Vector(poly.center)
        is_wheel = poly.material_index == tyre_mi or (
            poly.material_index == chrome_mi
            and min((cen - c).length_squared for c in centres.values()) < hub_capture2)
        if is_wheel:
            bn = nearest_bone(cen)
            for vi in poly.vertices:
                vert_bone[vi] = bn

    groups = {bn: obj.vertex_groups.new(name=bn) for bn in centres}
    body_vg = obj.vertex_groups.new(name="body")
    for v in me.vertices:
        (groups[vert_bone[v.index]] if v.index in vert_bone else body_vg).add(
            [v.index], 1.0, "REPLACE")

    obj.parent = rig
    m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig
    if demo:
        demo_pose_vehicle(rig)
    return rig


def reset_scene(prefix="Vehicle"):
    """Delete previously generated vehicles + their orphaned materials."""
    bpy.ops.object.select_all(action="DESELECT")
    for o in bpy.data.objects:
        if o.name.startswith(prefix):
            o.select_set(True)
    bpy.ops.object.delete()
    for m in list(bpy.data.materials):
        if m.name.startswith("veh_") and not m.users:
            bpy.data.materials.remove(m)


# ──────────────────────────────────────────────────────────────────────────────
# Codegen: emit a self-contained bpy script that builds ONE detailed vehicle.
# This is the bridge procedural_pipeline.py calls for its --detailed backend.
# It imports THIS module by absolute path inside Blender, so the geometry code
# stays the single source of truth (no duplication into the emitted script) —
# the same emit-a-runnable-script contract as base_models.build_base_code().
# ──────────────────────────────────────────────────────────────────────────────

_DETAILED_BODY = '''
import sys
import bpy
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import importlib
import vehicle_generator as _vg
importlib.reload(_vg)   # pick up live edits when re-run in a warm Blender

_gen = _vg.generate_plane if KIND == "plane" else _vg.generate_vehicle
obj = _gen(random_seed=SEED, params=PARAMS, name=NAME)

# generate_vehicle links the object to the scene's root collection; move it (and,
# when rigging, the armature) into our named collection so the pipeline's
# PROC_<slug> grouping stays consistent.
_coll = bpy.data.collections.get(COLL)
if _coll is None:
    _coll = bpy.data.collections.new(COLL)
    bpy.context.scene.collection.children.link(_coll)

def _move(o):
    for _c in list(o.users_collection):
        _c.objects.unlink(o)
    _coll.objects.link(o)

_move(obj)

_rig = None
if RIG:
    # the rigger bakes the mesh transform to origin; apply the X offset to the
    # rig afterwards so the bound mesh follows as a child.
    _rigfn = _vg.rig_plane if KIND == "plane" else _vg.rig_vehicle
    _rig = _rigfn(obj, params=PARAMS, name=NAME, demo=DEMO)
    _move(_rig)
    _rig.location.x += OX
    _root_name = _rig.name
else:
    obj.location.x += OX
    _root_name = obj.name

result = {"backend": "detailed_" + KIND, "object": obj.name, "root": _root_name,
          "collection": COLL, "parts": 1, "rigged": bool(RIG),
          "objects": [o.name for o in _coll.objects]}
print("[proc] detailed", KIND + ":", obj.name, "rigged:", bool(RIG), "in", COLL)

# Optional Unity-friendly FBX export (same settings as src/blender/mcp_backend).
if FBX:
    import os as _os2
    _os2.makedirs(_os2.path.dirname(FBX) or ".", exist_ok=True)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    _sel = [obj] + ([_rig] if _rig is not None else [])
    for _o in _sel:
        _o.select_set(True)
    bpy.context.view_layer.objects.active = _sel[0]
    if not hasattr(bpy.ops.export_scene, "fbx"):
        try:
            bpy.ops.preferences.addon_enable(module="io_scene_fbx")
        except Exception as _ex:
            print("[proc] FBX addon enable failed:", _ex)
    _has_anim = bool(bpy.data.actions)
    bpy.ops.export_scene.fbx(
        filepath=FBX, use_selection=True,
        apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
        axis_forward="-Z", axis_up="Y", bake_space_transform=True,
        add_leaf_bones=False, mesh_smooth_type="FACE",
        bake_anim=_has_anim, bake_anim_use_all_actions=_has_anim,
    )
    result["fbx"] = FBX.replace("\\\\", "/")
    result["tris"] = sum(len(p.vertices) - 2 for p in obj.data.polygons)
    print("[proc] exported FBX:", FBX)
'''

_DETAILED_MANIFEST = '''
import json as _json, os as _os
try:
    _p = _os.path.join(_OUT_DIR, COLL + ".result.json")
    _os.makedirs(_OUT_DIR, exist_ok=True)
    with open(_p, "w", encoding="utf-8") as _f:
        _json.dump(result, _f, indent=2)
    print("[proc] wrote", _p)
except Exception as _e:
    print("[proc] result write failed:", _e)
'''


def build_detailed_code(params, coll, ox=0.0, out_dir=None, name=None, seed_str="",
                        rig=False, demo=False, kind="car", fbx_path=None):
    """Return a self-contained bpy script that builds one high-detail asset with
    `params` in collection `coll` at X offset `ox`.

    kind     — "car" (generate_vehicle) or "plane" (generate_plane).
    rig      — also build the armature and skin-bind the mesh (drivable).
    demo     — pose the rig (spin wheels/prop + lift the front axle).
    fbx_path — when set, export a Unity-friendly FBX there (mesh + rig).
    out_dir  — append a tail that writes a result JSON (for the headless backend).
    name     — object name (defaults to the collection name).
    seed_str — random seed string for a repeatable result.
    """
    repo = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    name = name or coll
    kind = "plane" if kind == "plane" else "car"
    header = (
        '_REPO = r"{repo}"\n'
        "COLL = {c!r}\nOX = {x}\nNAME = {n!r}\nSEED = {s!r}\nKIND = {k!r}\n"
        "RIG = {r}\nDEMO = {dm}\nFBX = {fbx!r}\nPARAMS = {p}\n"
    ).format(repo=repo, c=coll, x=float(ox), n=name, s=seed_str, k=kind,
             r=bool(rig), dm=bool(demo), fbx=fbx_path, p=repr(params))
    tail = ""
    if out_dir:
        header = '_OUT_DIR = r"{d}"\n'.format(d=str(out_dir).replace("\\", "/")) + header
        tail = _DETAILED_MANIFEST
    return header + _DETAILED_BODY + tail


# ──────────────────────────────────────────────────────────────────────────────
# Run: generate a small fleet grid (like the original's screenshots) when this
# file is executed inside Blender. Importing it just exposes generate_vehicle().
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    reset_scene()
    # A few presets to prove the parameter space spans real vehicle classes.
    presets = [
        ("sedan",  {}),
        ("suv",    {"body_h": 1.05, "wheel_r": 0.60, "cabin_h": 0.80, "body_len": 4.6}),
        ("sports", {"body_h": 0.58, "cabin_h": 0.40, "rake": 0.40, "wheel_r": 0.40,
                    "wheel_w": 0.48, "body_len": 4.2}),
        ("truck",  {"body_h": 1.20, "body_w": 2.2, "body_len": 5.4, "wheel_r": 0.78,
                    "cabin_len": 0.40, "cabin_pos": 0.18}),
    ]
    for i, (label, prm) in enumerate(presets):
        obj = generate_vehicle(random_seed=label, params=prm, name="Vehicle_" + label)
        obj.location.x = i * 6.0   # space them out in a row
    print("[veh] generated", len(presets), "vehicles")

    # A couple of aircraft presets too (prop + jet).
    planes = [
        ("prop", {"jets": 0.0, "fus_len": 4.0, "wing_span": 6.0}),
        ("jet",  {"jets": 1.0, "fus_len": 7.5, "fus_w": 1.3, "wing_span": 9.0, "fin_h": 1.8}),
    ]
    for i, (label, prm) in enumerate(planes):
        obj = generate_plane(random_seed=label, params=prm, name="Plane_" + label)
        obj.location.x = i * 12.0
        obj.location.y = 12.0      # park them in a second row behind the cars
    print("[veh] generated", len(planes), "planes")
