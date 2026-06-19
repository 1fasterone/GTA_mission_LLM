"""
Parametric base-model library (the "starter model + manipulate" pipeline).

The lesson from the LLM-from-scratch tests: a local model can't compose recognisable
vehicles/characters from primitives (spatial reasoning is the wall, not code). So we
flip it — a human authors a believable PARAMETRIC base once (it's recognisable by
construction), and the LLM's only job shrinks to emitting a PARAMETER dict
("jeep with big tires" -> base=car, wheel_r big, ride high, boxy). Geometry quality
is guaranteed; the model just turns knobs. Same pattern as recipes.py / rig_templates.py,
extended to whole composite meshes.

Pieces:
  * `BASE_PARAM_SCHEMA` — per base, {param: (default, lo, hi)}; the LLM's allowed knobs.
  * `clamp_params` — validate/fill/clamp a raw param dict (robust like unity_schema).
  * `keyword_base` / `keyword_hints` — deterministic prompt→base + obvious knob nudges,
    so the pipeline works WITHOUT the LLM (graceful degradation / baseline).
  * `build_base_code` — a self-contained bpy script that builds `base` with `params`
    in a collection at an X offset (runs in live MCP Blender or headless, like the
    rest of the package).

Coordinate care baked into the authored builders (where the LLM failed): Z-up, parts
stand on the ground (z=0), wheels are side-facing cylinders (axle along X), slabs are
scaled cubes. Proven believable live on an 8GB 4060.
"""

from __future__ import annotations

import json
from typing import Optional

# ── parameter schemas: {param: (default, lo, hi)} ─────────────────────────────
BASE_PARAM_SCHEMA: dict[str, dict[str, tuple]] = {
    "car": {
        "body_w":      (1.7, 1.2, 2.4),
        "body_len":    (3.8, 2.8, 5.5),
        "body_h":      (0.60, 0.40, 1.20),
        "cabin_h":     (0.50, 0.30, 1.00),
        "wheel_r":     (0.42, 0.30, 0.95),
        "wheel_w":     (0.34, 0.20, 0.60),
        "ride":        (0.00, 0.00, 0.60),
        # structural knobs — these change the SILHOUETTE, not just the scale:
        "cabin_len":   (0.50, 0.30, 0.90),   # cabin length as a fraction of body_len
        "cabin_pos":   (0.00, -0.30, 0.30),  # cabin shift toward front(+) / rear(-)
        "bed":         (0.0, 0.0, 1.0),      # >=0.5 -> open pickup bed behind the cabin
        "wheel_pairs": (2, 2, 3),            # axles: 2 (normal) or 3 (6-wheel limo/truck)
        "rim":         (0.45, 0.0, 0.6),     # hub radius as a fraction of the tyre (0 = none)
    },
    "plane": {
        "fuselage_len": (5.0, 3.0, 9.0),
        "fuselage_r":   (0.50, 0.30, 1.00),
        "wing_span":    (6.0, 3.0, 12.0),
        "wing_chord":   (1.2, 0.6, 2.5),
        "fin_h":        (1.0, 0.5, 2.0),
    },
    "humanoid": {
        "height":     (1.8, 1.2, 2.6),
        "shoulder_w": (0.50, 0.30, 0.90),
        "hip_w":      (0.34, 0.20, 0.60),
        "head_r":     (0.13, 0.08, 0.22),
        "limb_r":     (0.08, 0.05, 0.16),
    },
    "bike": {
        "wheelbase":   (1.05, 0.60, 1.60),
        "wheel_r":     (0.34, 0.20, 0.55),
        "wheel_w":     (0.10, 0.06, 0.32),   # thin (road) -> fat (dirt/chopper)
        "frame_h":     (0.40, 0.25, 0.65),
        "handlebar_w": (0.50, 0.30, 0.80),
        "rim":         (0.55, 0.0, 0.70),
        # structural — choose the kind of bike:
        "wheels":      (2, 1, 2),            # 1 = unicycle, 2 = normal
        "engine":      (0.0, 0.0, 1.0),      # >=0.5 -> motorbike (engine block)
        "tank":        (0.0, 0.0, 1.0),      # >=0.5 -> fuel tank
        "pedals":      (1.0, 0.0, 1.0),      # >=0.5 -> pedals (bicycle/unicycle)
        "suspension":  (0.0, 0.0, 1.0),      # >=0.5 -> dirt-bike front forks
    },
}

BASE_NAMES = tuple(BASE_PARAM_SCHEMA.keys())

DEFAULT_COLOR: dict[str, tuple] = {
    "car":      (0.12, 0.32, 0.72),
    "plane":    (0.85, 0.85, 0.88),
    "humanoid": (0.80, 0.66, 0.55),
    "bike":     (0.70, 0.10, 0.10),
}

# prompt keyword -> base (first match wins, order matters; bike before car so
# "motorbike"/"dirt bike" don't get swallowed by a generic vehicle match)
_KEYWORD_BASE = [
    (("airplane", "aeroplane", "plane", "aircraft", "jet", "biplane", "fighter", "glider"), "plane"),
    (("bicycle", "motorbike", "motorcycle", "dirt bike", "unicycle", "scooter", "chopper",
      "moped", "bike"), "bike"),
    (("jeep", "car", "truck", "suv", "vehicle", "sedan", "buggy", "van", "auto"), "car"),
    (("human", "humanoid", "person", "character", "man", "woman", "robot", "biped", "figure"), "humanoid"),
]


def keyword_base(prompt: str) -> Optional[str]:
    """Deterministic prompt -> base, no LLM. None if nothing matches."""
    p = prompt.lower()
    for words, base in _KEYWORD_BASE:
        if any(w in p for w in words):
            return base
    return None


def keyword_hints(prompt: str, base: str) -> dict:
    """A few obvious deterministic knob nudges from the prompt (baseline before the
    LLM is wired). Conservative — only unmistakable cues."""
    p = prompt.lower()
    out: dict = {}
    if base == "car":
        if any(w in p for w in ("big tire", "big tyre", "big wheel", "offroad", "off-road", "monster", "jeep", "lifted")):
            out["wheel_r"] = 0.75
            out["ride"] = 0.35
            out["body_h"] = 0.85
        if "long" in p or "limo" in p:
            out["body_len"] = 4.6
    elif base == "plane":
        if "big" in p or "jumbo" in p or "large" in p:
            out["fuselage_len"] = 7.5
            out["wing_span"] = 9.0
        if "glider" in p:
            out["wing_span"] = 11.0
    elif base == "humanoid":
        if "tall" in p:
            out["height"] = 2.3
        if "thin" in p or "slim" in p:
            out["shoulder_w"] = 0.36
            out["limb_r"] = 0.06
        if "broad" in p or "buff" in p or "strong" in p:
            out["shoulder_w"] = 0.78
    elif base == "bike":
        if any(w in p for w in ("dirt", "off-road", "offroad", "motocross", "mountain")):
            out.update(wheel_r=0.40, wheel_w=0.24, suspension=1.0, engine=1.0, tank=1.0, pedals=0.0)
        if any(w in p for w in ("motorbike", "motorcycle", "chopper", "moped")):
            out.update(engine=1.0, tank=1.0, pedals=0.0, wheel_w=0.18)
        if "scooter" in p:
            out.update(engine=1.0, tank=0.0, pedals=0.0, wheel_r=0.26)
        if "unicycle" in p:
            out.update(wheels=1.0, pedals=1.0, engine=0.0)
        if "bicycle" in p:
            out.update(engine=0.0, tank=0.0, pedals=1.0, wheel_w=0.08)
    return out


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _clamp_color(v, default):
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            c = [float(x) for x in v[:3]]
        except (TypeError, ValueError):
            return default
        if max(c) > 1.0:                 # LLM sometimes emits 0-255 -> normalise
            c = [x / 255.0 for x in c]
        return tuple(max(0.0, min(1.0, x)) for x in c)
    return default


def clamp_params(base: str, raw: Optional[dict]) -> dict:
    """Fill defaults + clamp every known knob to its range; validate colour; drop
    unknown keys. A bad LLM token can never break the builder."""
    base = base if base in BASE_PARAM_SCHEMA else "car"
    raw = raw or {}
    schema = BASE_PARAM_SCHEMA[base]
    out = {}
    for name, (default, lo, hi) in schema.items():
        out[name] = _clamp(raw.get(name, default), lo, hi, default)
    out["color"] = _clamp_color(raw.get("color"), DEFAULT_COLOR[base])
    return out


# ── emitted bpy: shared helpers + per-base builders + runner ───────────────────

_HELPERS = '''
import bpy
from math import radians

def colour(o, rgb):
    m = bpy.data.materials.new("M"); m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
    o.data.materials.append(m)

def _coll():
    c = bpy.data.collections.get(COLL)
    if c is None:
        c = bpy.data.collections.new(COLL)
        bpy.context.scene.collection.children.link(c)
    return c

def _into(o):
    c = _coll()
    for cc in list(o.users_collection):
        cc.objects.unlink(o)
    c.objects.link(o)
    return o

def box(loc, scale, rgb, bevel=0.05):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = _into(bpy.context.active_object); o.scale = scale; colour(o, rgb)
    if bevel:
        bm = o.modifiers.new("bev", "BEVEL"); bm.width = bevel; bm.segments = 2
    return o

def cyl(loc, r, depth, rgb, rot=(0, 0, 0), verts=24):
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=depth, location=loc, vertices=verts)
    o = _into(bpy.context.active_object); o.rotation_euler = rot; colour(o, rgb)
    return o

def cone(loc, r, depth, rgb, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cone_add(radius1=r, depth=depth, location=loc)
    o = _into(bpy.context.active_object); o.rotation_euler = rot; colour(o, rgb)
    return o

def sphere(loc, r, rgb):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=loc)
    o = _into(bpy.context.active_object); colour(o, rgb)
    return o
'''

# Reusable FUNCTIONAL parts: each returns one named object whose ORIGIN is at the
# right pivot, so the engine/rig can drive it (a wheel spins about its axle, etc.).
# This is the part-library half of the pattern — assemblers compose these.
_PARTS = '''
def make_wheel(name, loc, r, w, rim=0.45):
    """A wheel as ONE object, origin at the axle centre, axle along X (so rotating
    it about its local axis spins it). Tyre + optional hub joined into one mesh."""
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=w, location=loc, vertices=24)
    tire = _into(bpy.context.active_object)
    tire.name = name
    tire.rotation_euler = (0, radians(90), 0)
    colour(tire, (0.05, 0.05, 0.05))
    if rim > 0.01:
        bpy.ops.mesh.primitive_cylinder_add(radius=r * rim, depth=w * 1.08, location=loc, vertices=12)
        hub = _into(bpy.context.active_object)
        hub.rotation_euler = (0, radians(90), 0)
        colour(hub, (0.55, 0.55, 0.58))
        bpy.ops.object.select_all(action="DESELECT")
        hub.select_set(True); tire.select_set(True)
        bpy.context.view_layer.objects.active = tire
        bpy.ops.object.join()          # merge hub into tyre; keeps the tyre's axle origin
    tire["role"] = "wheel"             # role tag survives name-collision renames
    tire["wheel_label"] = name
    return tire

def tube_between(a, b, r, rgb):
    """A cylinder spanning points a->b (for frames, forks, masts, limbs)."""
    import mathutils as _mu
    a = _mu.Vector(a); b = _mu.Vector(b)
    d = b - a
    length = max(0.02, d.length)
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=length, location=(a + b) * 0.5, vertices=12)
    o = _into(bpy.context.active_object)
    o.rotation_euler = d.to_track_quat("Z", "Y").to_euler()   # align local Z to the a->b direction
    colour(o, rgb)
    return o
'''

_CAR_SRC = '''
def build(p, ox):
    wr, ww = p["wheel_r"], p["wheel_w"]
    bw, bl, bh = p["body_w"], p["body_len"], p["body_h"]
    ch = p["cabin_h"]; rim = p.get("rim", 0.45)
    bed = p["bed"] >= 0.5
    pairs = max(2, min(3, int(round(p["wheel_pairs"]))))
    col = tuple(p["color"]); parts = []
    body_z = wr + p["ride"] + bh * 0.5 + 0.05
    # chassis + cabin (named parts, role-tagged so lookups survive name collisions)
    b = box((ox, 0, body_z), (bw, bl, bh), col); b.name = "body"; b["role"] = "body"; parts.append(b)
    clen = max(0.5, p["cabin_len"] * bl)
    cy = p["cabin_pos"] * (bl * 0.5)
    lim = max(0.0, bl * 0.5 - clen * 0.5)
    cy = max(-lim, min(lim, cy))
    cab_z = body_z + bh * 0.5 + ch * 0.5
    cab = box((ox, cy, cab_z), (bw * 0.88, clen, ch), col); cab.name = "cabin"; cab["role"] = "cabin"; parts.append(cab)
    # pickup bed
    if bed:
        cabin_back = cy - clen * 0.5
        rear_end = -bl * 0.5
        blen = cabin_back - rear_end
        if blen > 0.4:
            bd = box((ox, (cabin_back + rear_end) * 0.5, body_z + bh * 0.5 + 0.09),
                     (bw * 0.92, blen - 0.05, 0.18), col)
            bd.name = "bed"; bd["role"] = "bed"; parts.append(bd)
    # wheels as functional parts, named Wheel.<F/M/R><L/R>
    wx = bw * 0.5 + ww * 0.5 - 0.05
    fy = bl * 0.5 - wr - 0.1
    ry = -(bl * 0.5 - wr - 0.1)
    labels = {2: ["F", "R"], 3: ["F", "M", "R"]}[pairs]
    ys = [fy, ry] if pairs == 2 else [fy, (fy + ry) * 0.5, ry]
    for lab, ay in zip(labels, ys):
        for side, sx in (("L", 1), ("R", -1)):
            parts.append(make_wheel("Wheel." + lab + side, (ox + sx * wx, ay, wr), wr, ww, rim))
    return parts
'''

_PLANE_SRC = '''
def build(p, ox):
    fl, fr = p["fuselage_len"], p["fuselage_r"]
    span, chord, fin = p["wing_span"], p["wing_chord"], p["fin_h"]
    col = tuple(p["color"]); dark = (0.10, 0.10, 0.10); metal = (0.7, 0.72, 0.75); parts = []
    cz = fr + 0.5  # fuselage centreline height (parked, slight clearance)
    # fuselage: cylinder lying along Y (forward = -Y)
    parts.append(cyl((ox, 0, cz), fr, fl, col, rot=(radians(90), 0, 0), verts=20))
    # nose cone pointing forward (-Y)
    parts.append(cone((ox, -fl * 0.5 - fr * 0.8, cz), fr, fr * 1.6, col, rot=(radians(-90), 0, 0)))
    # main wings: wide thin slab across X
    parts.append(box((ox, 0, cz), (span, chord, 0.08), col))
    # horizontal stabiliser near the tail (+Y)
    parts.append(box((ox, fl * 0.42, cz), (span * 0.4, chord * 0.6, 0.06), col))
    # vertical fin standing up at the tail
    parts.append(box((ox, fl * 0.45, cz + fin * 0.5), (0.08, chord * 0.6, fin), col))
    # propeller: hub + two crossed blades at the nose
    hub = (ox, -fl * 0.5 - fr * 1.4, cz)
    parts.append(cyl(hub, fr * 0.35, 0.2, metal, rot=(radians(90), 0, 0)))
    parts.append(box((hub[0], hub[1], cz), (0.06, 0.05, fr * 2.6), dark))
    parts.append(box((hub[0], hub[1], cz), (fr * 2.6, 0.05, 0.06), dark))
    return parts
'''

_HUMANOID_SRC = '''
def build(p, ox):
    """T-pose humanoid built from bone-aligned segments. Every part is tagged with
    the EXACT Unity HumanBodyBones name it belongs to (o["bone"]) so the rigger can
    skin it 1:1 to a real humanoid skeleton — the result imports as a valid Unity
    Humanoid avatar (no "Hips not found"). Arms are horizontal along X (T-pose) and
    the figure faces +Y (Blender) -> +Z (Unity) so auto-mapping succeeds."""
    h = p["height"]; sw = p["shoulder_w"]; hw = p["hip_w"]
    hr = p["head_r"]; lr = p["limb_r"]
    skin = tuple(p["color"]); parts = []

    # vertical landmarks (ground z=0), proportioned off total height
    ankle_z = 0.05 * h
    knee_z  = 0.28 * h
    hip_z   = 0.52 * h
    chest_z = 0.70 * h
    shldr_z = 0.82 * h
    neck_z  = 0.87 * h
    # horizontal landmarks
    hipx   = max(0.06, hw * 0.5)
    shx    = max(0.10, sw * 0.5)
    arm_u  = max(0.18, sw * 0.55)            # upper-arm length
    arm_l  = max(0.16, sw * 0.50)            # lower-arm length
    elbowx = shx + arm_u
    wristx = elbowx + arm_l
    handx  = wristx + max(0.10, sw * 0.22)
    foot_fwd = lr * 3.2

    # share the landmarks with rig_model (runs later in the same module scope)
    global LANDMARKS
    LANDMARKS = {"h": h, "head_r": hr, "ankle_z": ankle_z, "knee_z": knee_z,
                 "hip_z": hip_z, "chest_z": chest_z, "shldr_z": shldr_z,
                 "neck_z": neck_z, "hipx": hipx, "shx": shx, "elbowx": elbowx,
                 "wristx": wristx, "handx": handx, "foot_fwd": foot_fwd}

    def tag(o, bone):
        o["bone"] = bone; o["role"] = "human"; parts.append(o); return o

    # torso + head
    tag(box((ox, 0, hip_z + 0.04 * h), (hw * 1.5, lr * 2.4, 0.12 * h), skin), "Hips")
    tag(box((ox, 0, (hip_z + shldr_z) * 0.5), (sw, lr * 2.6, shldr_z - hip_z), skin), "Spine")
    tag(cyl((ox, 0, (shldr_z + neck_z) * 0.5), lr * 0.9, max(0.04, neck_z - shldr_z), skin), "Neck")
    tag(sphere((ox, 0, neck_z + hr), hr, skin), "Head")

    # legs (vertical) + feet (forward +Y)
    for sx, pre in ((1, "Left"), (-1, "Right")):
        x = ox + sx * hipx
        tag(tube_between((x, 0, hip_z), (x, 0, knee_z), lr, skin), pre + "UpperLeg")
        tag(tube_between((x, 0, knee_z), (x, 0, ankle_z), lr * 0.85, skin), pre + "LowerLeg")
        tag(box((x, foot_fwd * 0.4, ankle_z * 0.5), (lr * 2.0, foot_fwd, ankle_z), skin), pre + "Foot")

    # arms (T-pose, horizontal along X) + hands
    for sx, pre in ((1, "Left"), (-1, "Right")):
        s = ox + sx * shx; e = ox + sx * elbowx; w = ox + sx * wristx; hd = ox + sx * handx
        tag(tube_between((s, 0, shldr_z), (e, 0, shldr_z), lr * 0.95, skin), pre + "UpperArm")
        tag(tube_between((e, 0, shldr_z), (w, 0, shldr_z), lr * 0.85, skin), pre + "LowerArm")
        tag(box(((w + hd) * 0.5, 0, shldr_z), (abs(hd - w), lr * 2.2, lr * 1.2), skin), pre + "Hand")
    return parts
'''

_BIKE_SRC = '''
def build(p, ox):
    wb = p["wheelbase"]; wr = p["wheel_r"]; ww = p["wheel_w"]; fh = p["frame_h"]
    hbw = p["handlebar_w"]; rim = p.get("rim", 0.55)
    n = max(1, min(2, int(round(p["wheels"]))))
    engine = p["engine"] >= 0.5; tank = p["tank"] >= 0.5
    pedals = p["pedals"] >= 0.5; susp = p["suspension"] >= 0.5
    col = tuple(p["color"]); metal = (0.30, 0.30, 0.32); parts = []
    seat_z = wr + fh
    if n == 1:
        # unicycle: one wheel, seat post, seat, pedals on the axle
        parts.append(make_wheel("Wheel.C", (ox, 0, wr), wr, ww, rim))
        post = tube_between((ox, 0, wr), (ox, 0, seat_z + 0.1), 0.04, metal); post["role"] = "frame"; parts.append(post)
        st = box((ox, 0, seat_z + 0.14), (0.28, 0.12, 0.07), col); st.name = "seat"; st["role"] = "frame"; parts.append(st)
        if pedals:
            for sx in (1, -1):
                pd = box((ox + sx * (ww * 0.5 + 0.12), 0, wr), (0.16, 0.06, 0.04), metal); pd["role"] = "frame"; parts.append(pd)
        return parts
    front_y = wb * 0.5; rear_y = -wb * 0.5
    head = (ox, front_y - 0.05, wr + fh * 0.95)
    bb = (ox, rear_y * 0.25, wr * 0.6)            # bottom bracket / engine area
    parts.append(make_wheel("Wheel.F", (ox, front_y, wr), wr, ww, rim))
    parts.append(make_wheel("Wheel.R", (ox, rear_y, wr), wr, ww, rim))
    # fork (steers) + optional suspension leg
    fk = tube_between((ox, front_y, wr), head, 0.045, metal); fk.name = "fork"; fk["role"] = "fork"; parts.append(fk)
    if susp:
        fk2 = tube_between((ox, front_y, wr), (head[0], head[1], head[2] - 0.06), 0.06, (0.5, 0.5, 0.55)); fk2["role"] = "fork"; parts.append(fk2)
    # handlebars (bar along X), steer
    hb = cyl(head, 0.025, hbw, metal, rot=(0, radians(90), 0)); hb.name = "handlebar"; hb["role"] = "handlebar"; parts.append(hb)
    # frame tubes: head->bb, bb->seat, seat->rear hub, bb->rear hub (chain stay)
    seat_top = (ox, rear_y * 0.2, seat_z)
    for a, b, rr in ((head, bb, 0.045), (bb, seat_top, 0.04), (seat_top, (ox, rear_y, wr), 0.035), (bb, (ox, rear_y, wr), 0.035)):
        t = tube_between(a, b, rr, col); t["role"] = "frame"; parts.append(t)
    st = box((ox, rear_y * 0.2, seat_z + 0.06), (0.24, 0.14, 0.07), col); st.name = "seat"; st["role"] = "frame"; parts.append(st)
    if engine:
        eg = box((ox, bb[1], wr * 0.85), (0.30, 0.36, 0.34), metal); eg.name = "engine"; eg["role"] = "frame"; parts.append(eg)
    if tank:
        tk = box((ox, (bb[1] + head[1]) * 0.5, seat_z * 0.95), (0.24, 0.5, 0.22), col); tk.name = "tank"; tk["role"] = "frame"; parts.append(tk)
    if pedals and not engine:
        for sx in (1, -1):
            pd = box((ox + sx * 0.14, bb[1], bb[2]), (0.16, 0.06, 0.04), metal); pd["role"] = "frame"; parts.append(pd)
    return parts
'''

_BUILDERS = {"car": _CAR_SRC, "plane": _PLANE_SRC, "humanoid": _HUMANOID_SRC, "bike": _BIKE_SRC}

# ── riggers: define `rig_model(parts, ox)` per base (game-functional skeletons) ──
# Car: a root + body bone + one bone per wheel (axle along +X). Each part is rigidly
# skin-bound (single vertex group, weight 1.0) to its bone, so a wheel bone's roll
# spins that wheel and its Z move = suspension travel. Exports as a rigged FBX.
_CAR_RIG = '''
def rig_model(parts, ox):
    body = next((o for o in parts if o.get("role") == "body"), None)
    if body is None:
        return None
    wheels = [o for o in parts if o.get("role") == "wheel"]
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm)
    _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones
    root = eb.new("root"); root.head = (ox, 0, 0.02); root.tail = (ox, 0, 0.32)
    bb = eb.new("body"); bz = body.location.z
    bb.head = (ox, 0, bz); bb.tail = (ox, 0.6, bz); bb.parent = root
    labels = {}
    for w in wheels:
        lab = w.get("wheel_label", w.name)
        bn = eb.new(lab)                                      # bone names unique within this rig
        bn.head = (w.location.x, w.location.y, w.location.z)
        bn.tail = (w.location.x + 0.3, w.location.y, w.location.z)   # axle along +X
        bn.parent = root
        labels[w.name] = lab
    bpy.ops.object.mode_set(mode="OBJECT")

    def bind(obj, bone):
        if obj is None:
            return
        obj.parent = rig
        vg = obj.vertex_groups.new(name=bone)
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig

    bind(body, "body")
    bind(next((o for o in parts if o.get("role") == "cabin"), None), "body")
    bind(next((o for o in parts if o.get("role") == "bed"), None), "body")
    for w in wheels:
        bind(w, labels[w.name])
    return rig
'''

_BIKE_RIG = '''
def rig_model(parts, ox):
    wheels = [o for o in parts if o.get("role") == "wheel"]
    if not wheels:
        return None
    frame = [o for o in parts if o.get("role") == "frame"]
    fork = [o for o in parts if o.get("role") == "fork"]
    bars = [o for o in parts if o.get("role") == "handlebar"]
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm); _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones
    root = eb.new("root"); root.head = (ox, 0, 0.02); root.tail = (ox, 0, 0.3)
    fr = eb.new("frame"); fr.head = (ox, 0, 0.3); fr.tail = (ox, 0.4, 0.3); fr.parent = root
    wf = next((o for o in wheels if o.get("wheel_label") == "Wheel.F"), None)
    rr = next((o for o in wheels if o.get("wheel_label") == "Wheel.R"), None)
    wc = next((o for o in wheels if o.get("wheel_label") == "Wheel.C"), None)
    steer = None
    if wf is not None or fork or bars:
        hy = (bars[0].location.y if bars else (wf.location.y if wf else ox))
        steer = eb.new("steer"); steer.head = (ox, hy, 0.3); steer.tail = (ox, hy, 0.9); steer.parent = root
    labels = {}
    def wbone(w, parent):
        bn = eb.new(w.get("wheel_label", w.name))
        bn.head = (w.location.x, w.location.y, w.location.z)
        bn.tail = (w.location.x + 0.3, w.location.y, w.location.z)
        bn.parent = parent
        labels[w.name] = bn.name
    if wf is not None: wbone(wf, steer or root)      # front wheel turns WITH the steer bone
    if rr is not None: wbone(rr, root)
    if wc is not None: wbone(wc, root)
    bpy.ops.object.mode_set(mode="OBJECT")
    def bind(obj, bone):
        if obj is None: return
        obj.parent = rig
        vg = obj.vertex_groups.new(name=bone); vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig
    for o in frame: bind(o, "frame")
    for o in fork: bind(o, "steer" if steer else "frame")
    for o in bars: bind(o, "steer" if steer else "frame")
    for w in wheels: bind(w, labels.get(w.name, "frame"))
    return rig
'''

# Humanoid: build the standard Unity HumanBodyBones skeleton (exact names so the
# importer's avatar auto-mapper recognises them), then rigidly skin each tagged
# mesh segment to its bone (weight 1.0). Required bones present: Hips, Spine, Head,
# {Left,Right}{UpperLeg,LowerLeg,Foot,UpperArm,LowerArm,Hand} -> a VALID Humanoid
# avatar, so any humanoid clip (Mixamo-style Run/Jump) retargets onto it.
_HUMANOID_RIG = '''
def rig_model(parts, ox):
    import mathutils
    L = LANDMARKS
    h = L["h"]
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm); _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones

    def mk(name, head, tail, parent=None):
        b = eb.new(name)
        b.head = mathutils.Vector(head)
        b.tail = mathutils.Vector(tail)
        if parent is not None:
            b.parent = eb[parent]
        return b

    hip_z = L["hip_z"]; chest_z = L["chest_z"]; shldr_z = L["shldr_z"]; neck_z = L["neck_z"]
    # spine chain (Hips -> Spine -> Chest -> Neck -> Head)
    mk("Hips",  (ox, 0, hip_z),            (ox, 0, hip_z + 0.06 * h))
    mk("Spine", (ox, 0, hip_z + 0.06 * h), (ox, 0, chest_z), "Hips")
    mk("Chest", (ox, 0, chest_z),          (ox, 0, shldr_z), "Spine")
    mk("Neck",  (ox, 0, shldr_z),          (ox, 0, neck_z),  "Chest")
    mk("Head",  (ox, 0, neck_z),           (ox, 0, neck_z + 2.0 * L.get("head_r", 0.13)), "Neck")

    for sx, pre in ((1, "Left"), (-1, "Right")):
        x = ox + sx * L["hipx"]
        mk(pre + "UpperLeg", (x, 0, hip_z),       (x, 0, L["knee_z"]),  "Hips")
        mk(pre + "LowerLeg", (x, 0, L["knee_z"]), (x, 0, L["ankle_z"]), pre + "UpperLeg")
        mk(pre + "Foot",     (x, 0, L["ankle_z"]),(x, L["foot_fwd"], L["ankle_z"] * 0.4), pre + "LowerLeg")
        sh = ox + sx * L["shx"]
        mk(pre + "Shoulder", (ox + sx * 0.02, 0, shldr_z), (sh, 0, shldr_z), "Chest")
        mk(pre + "UpperArm", (sh, 0, shldr_z),             (ox + sx * L["elbowx"], 0, shldr_z), pre + "Shoulder")
        mk(pre + "LowerArm", (ox + sx * L["elbowx"], 0, shldr_z), (ox + sx * L["wristx"], 0, shldr_z), pre + "UpperArm")
        mk(pre + "Hand",     (ox + sx * L["wristx"], 0, shldr_z), (ox + sx * L["handx"], 0, shldr_z), pre + "LowerArm")

    bpy.ops.object.mode_set(mode="OBJECT")

    def bind(obj, bone):
        obj.parent = rig
        vg = obj.vertex_groups.new(name=bone)
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig

    for o in parts:
        b = o.get("bone")
        if b:
            bind(o, b)
    return rig
'''

_NO_RIG = '''
def rig_model(parts, ox):
    return None
'''

_RIGGERS = {"car": _CAR_RIG, "bike": _BIKE_RIG, "humanoid": _HUMANOID_RIG}

# Pose the rig into a demo: roll every wheel bone (spin) + lift the front axle
# (suspension), proving the parts are bone-driven and move independently.
_DEMO = '''
def demo_pose(rig, spin_deg=35.0, lift=0.22, steer_deg=22.0):
    import math as _m
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="POSE")
    for pb in rig.pose.bones:
        if pb.name.startswith("Wheel."):
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, _m.radians(spin_deg), 0.0)   # roll about axle (bone Y = +X)
            if pb.name[6:7] == "F":                                # front axle -> suspension lift
                pb.location = (0.0, 0.0, lift)
        elif pb.name == "steer":
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, _m.radians(steer_deg), 0.0)  # yaw about the (vertical) steer bone
    bpy.ops.object.mode_set(mode="OBJECT")
'''

_RUN = '''
root = bpy.data.objects.new(COLL + "_root", None)
_into(root)
_parts = build(PARAMS, OX)
_rig = rig_model(_parts, OX) if RIG else None
if _rig is not None:
    _rig.parent = root
for _o in _parts:
    if _o.parent is None:
        _o.parent = root
if RIG and DEMO and _rig is not None:
    demo_pose(_rig)
result = {"base": BASE, "parts": len(_parts), "rigged": _rig is not None,
          "collection": COLL, "objects": [o.name for o in _coll().objects]}
print("[proc] built", len(_parts), "parts; rigged:", _rig is not None)
'''

# Tail that writes `result` to disk for the headless backend (mirrors mcp_backend).
_MANIFEST_TAIL = '''
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


def build_base_code(base: str, params: dict, coll: str, ox: float = 0.0,
                    out_dir: Optional[str] = None, rig: bool = False,
                    demo: bool = False) -> str:
    """Self-contained bpy script that builds `base` with `params` in collection
    `coll` at X offset `ox`.

    rig  — also build the base's skeleton and skin-bind the parts (game-functional).
    demo — pose the rig (spin wheels + suspension) to demonstrate it moves.
    out_dir — append a tail that writes a result JSON (for the headless backend).
    """
    base = base if base in _BUILDERS else "car"
    header = (
        "BASE = {b!r}\nCOLL = {c!r}\nOX = {x}\nRIG = {r}\nDEMO = {dm}\nPARAMS = {p}\n".format(
            b=base, c=coll, x=float(ox), r=bool(rig), dm=bool(demo), p=json.dumps(params))
    )
    tail = ""
    if out_dir:
        header = '_OUT_DIR = r"{d}"\n'.format(d=str(out_dir).replace("\\", "/")) + header
        tail = _MANIFEST_TAIL
    rigger = _RIGGERS.get(base, _NO_RIG)
    return (header + _HELPERS + _PARTS + "\n" + _BUILDERS[base] + "\n"
            + rigger + _DEMO + "\n" + _RUN + tail)
