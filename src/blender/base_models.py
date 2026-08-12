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

Robustness is scoped to VALUES, not to the base itself: a bad LLM token in a param
dict is clamped away, but an unknown base raises `UnknownBaseError` rather than
quietly building something else. Both entry points used to substitute "furniture"
for anything they did not recognise, which turned `keyword_base("a jeep")` -> "car"
(routed correctly, built by vehicle_generator.py) into a silently-emitted TABLE,
reported as base "car". That is the failure mode CLAUDE.md's "abstain loudly, never
silently default" rule exists to prevent: the wrong model is harder to diagnose than
a refusal naming the remedy.

Coordinate care baked into the authored builders (where the LLM failed): Z-up, parts
stand on the ground (z=0), wheels are side-facing cylinders (axle along X), slabs are
scaled cubes. Proven believable live on an 8GB 4060.
"""

from __future__ import annotations

import json
from typing import Optional

# ── parameter schemas: {param: (default, lo, hi)} ─────────────────────────────
# "car" is NOT a key here: it is built by repo-root vehicle_generator.py (real
# PBR materials, edge-loop-sculpted body — see _VG_VARIANTS in src/micro/model.py),
# not by this file. base_models used to carry a second, CLI-unreachable
# primitive-composite car builder (_CAR_SRC/_CAR_RIG); it was deleted rather than
# maintained as a dead duplicate — see docs/PROCEDURAL_BASES.md.
BASE_PARAM_SCHEMA: dict[str, dict[str, tuple]] = {
    "plane": {
        "fuselage_len": (5.0, 3.0, 9.0),
        "fuselage_r":   (0.50, 0.30, 1.00),
        "wing_span":    (6.0, 3.0, 12.0),
        "wing_chord":   (1.2, 0.6, 2.5),
        "fin_h":        (1.0, 0.5, 2.0),
    },
    "boat": {
        "hull_len":   (6.0, 3.0, 16.0),
        "hull_beam":  (2.0, 1.0, 5.0),
        "hull_depth": (1.2, 0.6, 3.0),
        "deck_h":     (0.12, 0.05, 0.40),
        "mast_h":     (4.0, 0.0, 9.0),
        # structural — choose the kind of boat (rowboat / sailboat / motorboat / tug):
        "masts":   (1, 0, 3),            # number of masts (0 = none)
        "sails":   (1.0, 0.0, 1.0),      # >=0.5 -> square sails on the masts
        "prop":    (0.0, 0.0, 1.0),      # >=0.5 -> motor propeller (spins)
        "cabin":   (0.0, 0.0, 1.0),      # >=0.5 -> wheelhouse / cabin
        "rudder":  (1.0, 0.0, 1.0),      # >=0.5 -> steerable rudder (yaws)
        "funnel":  (0.0, 0.0, 1.0),      # >=0.5 -> smokestack (steamer / tug)
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
        # tyre width, metres. The DEFAULT bike is a bicycle (pedals=1, engine=0),
        # so the default tyre must be a bicycle's: 45 mm is a city/hybrid tyre.
        # The old 0.10 default put a 100 mm motorcycle tyre on every pedal bike,
        # and lo=0.06 made a road tyre inexpressible at any knob setting.
        "wheel_w":     (0.045, 0.022, 0.32),  # road 25 mm -> chopper 320 mm
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
    "furniture": {
        "width":    (1.20, 0.30, 3.00),    # X span of the piece
        "depth":    (0.70, 0.25, 2.20),    # Y span
        "height":   (0.75, 0.30, 2.20),    # Z of the primary surface / total height
        "top_t":    (0.06, 0.02, 0.20),    # surface (top / seat / lid) slab thickness
        "leg_r":    (0.05, 0.02, 0.18),    # leg radius
        # structural — choose the kind of furniture (table/chair/shelf/dresser/...):
        "legs":     (4, 0, 4),             # 0=solid carcass, 1=pedestal, 3, 4=corner legs
        "seat":     (0.0, 0.0, 1.0),       # >=0.5 -> the surface is a seat (chair/stool/bench/sofa)
        "back":     (0.0, 0.0, 1.0),       # >=0.5 -> backrest above the seat
        "shelves":  (0, 0, 6),             # >0 -> open shelving (bookcase); implies a carcass
        "drawers":  (0, 0, 6),             # >0 -> stacked drawer fronts (desk/dresser)
        "cushion":  (0.0, 0.0, 1.0),       # >=0.5 -> padded look (thicker seat / mattress)
    },
    "weapon": {
        "length":     (0.90, 0.20, 1.40),  # overall size (receiver + furniture)
        "caliber":    (0.03, 0.015, 0.08), # bore / barrel radius
        "barrel_len": (0.45, 0.10, 0.95),  # barrel length forward (+Y) of the receiver
        # structural — the attachment loadout (scopes, stocks, barrels, muzzles, ...):
        "stock":      (1, 0, 2),           # 0=none (pistol), 1=fixed, 2=skeleton/folding
        "muzzle":     (0, 0, 2),           # 0=bare, 1=muzzle brake, 2=suppressor
        "magazine":   (1, 0, 2),           # 0=none, 1=box mag, 2=drum
        "scope":      (0.0, 0.0, 1.0),     # >=0.5 -> optic on the top rail
        "grip":       (1.0, 0.0, 1.0),     # >=0.5 -> pistol grip
        "foregrip":   (0.0, 0.0, 1.0),     # >=0.5 -> vertical foregrip under the handguard
    },
}

BASE_NAMES = tuple(BASE_PARAM_SCHEMA.keys())

DEFAULT_COLOR: dict[str, tuple] = {
    "plane":    (0.85, 0.85, 0.88),
    "boat":     (0.32, 0.20, 0.12),     # varnished wood hull
    "humanoid": (0.80, 0.66, 0.55),
    "bike":     (0.70, 0.10, 0.10),
    "furniture": (0.45, 0.30, 0.16),    # warm wood
    "weapon":   (0.15, 0.15, 0.16),     # gunmetal / black polymer
}

# Humanoid material palette (from "Fully Rigged Humanoid Character.md"). The LLM
# can override any slot via params["palette"]; "skin" defaults to the model colour
# so a "green android" still tints the skin. Every slot is colour-validated/clamped.
HUMANOID_PALETTE: dict[str, tuple] = {
    "skin":  (0.80, 0.66, 0.55),
    "shirt": (0.20, 0.40, 0.80),
    "pants": (0.15, 0.15, 0.25),
    "shoe":  (0.10, 0.10, 0.10),
    "hair":  (0.12, 0.08, 0.05),
    "eye":   (0.92, 0.92, 0.94),
    "pupil": (0.05, 0.05, 0.05),
}

# prompt keyword -> base (first match wins, order matters; bike before car so
# "motorbike"/"dirt bike" don't get swallowed by a generic vehicle match)
_KEYWORD_BASE = [
    (("airplane", "aeroplane", "plane", "aircraft", "jet", "biplane", "fighter", "glider"), "plane"),
    (("boat", "ship", "sailboat", "sailing", "yacht", "tugboat", "tug", "ferry", "canoe",
      "kayak", "dinghy", "rowboat", "trawler", "galleon", "schooner", "barge", "catamaran",
      "watercraft", "vessel"), "boat"),
    (("bicycle", "motorbike", "motorcycle", "dirt bike", "unicycle", "scooter", "chopper",
      "moped", "bike"), "bike"),
    (("jeep", "car", "truck", "suv", "vehicle", "sedan", "buggy", "van", "auto"), "car"),
    (("furniture", "table", "desk", "chair", "stool", "bench", "sofa", "couch",
      "shelf", "bookcase", "bookshelf", "cabinet", "dresser", "drawer", "nightstand",
      "wardrobe", "ottoman", "bed"), "furniture"),
    (("weapon", "firearm", "gun", "pistol", "handgun", "revolver", "rifle", "carbine",
      "sniper", "shotgun", "smg", "submachine", "musket", "blaster", "sidearm"), "weapon"),
    (("human", "humanoid", "person", "character", "man", "woman", "robot", "biped", "figure"), "humanoid"),
]


# Bases that keyword_base can legitimately return but that this module does NOT
# build — named in the abstain message so a caller landing on one is told where the
# builder actually lives instead of just that it failed. "car" is the only one: it
# routes here by keyword and is built by vehicle_generator.py's detailed bmesh
# builder (see BASE_PARAM_SCHEMA's header note).
BASES_BUILT_ELSEWHERE: dict[str, str] = {
    "car": ("vehicle_generator.py (welded bmesh + real PBR materials). Reach it with "
            '`python main.py unity-model "<prompt>"` or '
            '`python procedural_pipeline.py "<prompt>" --detailed`'),
}


class UnknownBaseError(ValueError):
    """A base has no schema/builder here. Subclasses ValueError so existing
    `except ValueError` guards around param handling still catch it."""


def _require_base(base: str, known, what: str) -> None:
    """Abstain loudly on an unrecognised base. Both entry points route through here
    so neither can drift back to a silent substitution."""
    if base in known:
        return
    where = BASES_BUILT_ELSEWHERE.get(base)
    fix = (f"build it with {where}" if where else
           f"pick one of the implemented bases, or add {base!r} to BASE_PARAM_SCHEMA "
           "and _BUILDERS")
    raise UnknownBaseError(
        f"[base.strict] base {base!r} {what} in base_models.py. "
        f"Implemented here: {', '.join(sorted(known))}. Fix: {fix}. "
        "This does not fall back to furniture - a prompt that asked for a vehicle "
        "and silently received a table is harder to diagnose than this error."
    )


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
    if base == "plane":
        if "big" in p or "jumbo" in p or "large" in p:
            out["fuselage_len"] = 7.5
            out["wing_span"] = 9.0
        if "glider" in p:
            out["wing_span"] = 11.0
    elif base == "boat":
        # kind of boat (later cues win; default stays a small single-mast sailboat)
        if any(w in p for w in ("sailboat", "sailing", "schooner", "galleon", "catamaran", "yacht")):
            out.update(masts=2 if any(w in p for w in ("schooner", "galleon")) else 1,
                       sails=1.0, prop=0.0)
        if any(w in p for w in ("motorboat", "speedboat", "powerboat", "trawler", "ferry")):
            out.update(masts=0, sails=0.0, prop=1.0, cabin=1.0)
        if any(w in p for w in ("tug", "tugboat", "steamer", "barge")):
            out.update(masts=0, sails=0.0, prop=1.0, cabin=1.0, funnel=1.0, hull_beam=2.6)
        if any(w in p for w in ("rowboat", "canoe", "kayak", "dinghy")):
            out.update(masts=0, sails=0.0, prop=0.0, cabin=0.0, rudder=0.0,
                       hull_len=3.6, hull_beam=1.3, hull_depth=0.7)
        if any(w in p for w in ("big", "large", "cargo", "galleon", "ferry")):
            out.update(hull_len=11.0, hull_beam=3.4, hull_depth=2.2)
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
            out.update(engine=1.0, tank=0.0, pedals=0.0, wheel_r=0.26, wheel_w=0.10)
        if "unicycle" in p:
            out.update(wheels=1.0, pedals=1.0, engine=0.0)
        if "bicycle" in p:
            # no wheel_w here: the schema default is already a bicycle tyre, and
            # forcing one would undo the off-road cue above — "mountain bicycle"
            # should keep its 240 mm knobblies AND get pedals back.
            out.update(engine=0.0, tank=0.0, pedals=1.0)
    elif base == "furniture":
        # kind of piece (later cues win, so generic "table" stays a clean default)
        if "table" in p:
            out.update(width=1.6, depth=0.9, height=0.75, legs=4)
        if "desk" in p:
            out.update(width=1.4, depth=0.7, height=0.75, legs=4, drawers=3)
        if "stool" in p:
            out.update(width=0.4, depth=0.4, height=0.6, seat=1.0, back=0.0, legs=4)
        if "chair" in p:
            out.update(width=0.5, depth=0.5, height=0.46, seat=1.0, back=1.0, legs=4)
        if "bench" in p:
            out.update(width=1.6, depth=0.45, height=0.45, seat=1.0, back=0.0, legs=4)
        if "sofa" in p or "couch" in p:
            out.update(width=2.0, depth=0.9, height=0.45, seat=1.0, back=1.0, legs=4, cushion=1.0)
        if any(w in p for w in ("shelf", "bookcase", "bookshelf")):
            out.update(width=0.9, depth=0.32, height=1.8, top_t=0.04, legs=0, shelves=4)
        if any(w in p for w in ("dresser", "drawers", "cabinet", "nightstand", "wardrobe")):
            out.update(width=1.0, depth=0.5, height=0.9, legs=0, drawers=4)
        if "bed" in p:
            # seat=1 is what turns `cushion` into real padding (the builder gates
            # the upholstered path on seat AND cushion). Without it a bed came out
            # a bare moulded slab on legs — a low table, not a mattress.
            out.update(width=1.6, depth=2.0, height=0.5, legs=4, cushion=1.0, seat=1.0)
        if "round" in p or "pedestal" in p:
            out["legs"] = 1
    elif base == "weapon":
        # weapon class (later cues win; default stays a generic rifle-ish receiver)
        if any(w in p for w in ("pistol", "handgun", "sidearm")):
            out.update(length=0.32, barrel_len=0.14, stock=0, magazine=1, foregrip=0)
        if "revolver" in p:
            out.update(length=0.32, barrel_len=0.16, stock=0, magazine=0, foregrip=0)
        if "smg" in p or "submachine" in p:
            out.update(length=0.6, barrel_len=0.25, stock=2, magazine=1)
        if any(w in p for w in ("rifle", "carbine", "assault", "ar-15", "ar15")):
            out.update(length=0.95, barrel_len=0.5, stock=1, magazine=1, foregrip=1)
        if "sniper" in p or "marksman" in p:
            out.update(length=1.2, barrel_len=0.85, stock=1, scope=1, magazine=1)
        if "shotgun" in p:
            out.update(length=1.0, barrel_len=0.6, caliber=0.05, stock=1, magazine=0)
        if any(w in p for w in ("machine gun", "machinegun", "lmg")):
            out.update(length=1.1, barrel_len=0.7, stock=1, magazine=2, foregrip=1)
        if "suppress" in p or "silenc" in p:
            out["muzzle"] = 2
        if "brake" in p or "compensator" in p:
            out["muzzle"] = 1
        if "scope" in p or "scoped" in p or "optic" in p:
            out["scope"] = 1
        if "drum" in p:
            out["magazine"] = 2
    return out


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _clamp_palette(raw, skin_default):
    """Validate/clamp every humanoid material slot; fill missing slots with the
    authored defaults. ``skin`` falls back to the model colour, not the palette
    default, so colour cues (e.g. 'green android') still tint the skin."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for slot, default in HUMANOID_PALETTE.items():
        base_default = skin_default if slot == "skin" else default
        out[slot] = _clamp_color(raw.get(slot), base_default)
    return out


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
    unknown keys. A bad LLM token in a VALUE can never break the builder — but an
    unknown `base` raises rather than silently returning another base's knobs."""
    _require_base(base, BASE_PARAM_SCHEMA, "has no parameter schema")
    raw = raw or {}
    schema = BASE_PARAM_SCHEMA[base]
    out = {}
    for name, (default, lo, hi) in schema.items():
        out[name] = _clamp(raw.get(name, default), lo, hi, default)
    out["color"] = _clamp_color(raw.get("color"), DEFAULT_COLOR[base])
    if base == "humanoid":
        out["palette"] = _clamp_palette(raw.get("palette"), out["color"])
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
def _wheel_ring(obj_name, center, radius, width, rgb, groove, end_notch):
    """One ring-extruded band (tyre or hub): ngon_ring(axis='X') -> extrude_ring
    to width -> optional alternating tread grooves (a full-radius contact-patch
    arc is kept at the bottom so the wheel still sits flush on the ground,
    regardless of segment count) -> optional recessed spoke panels on the two
    end caps. Real bmesh throughout — no primitive_cylinder_add."""
    import bmesh, mathutils
    from math import atan2 as _atan2
    center = mathutils.Vector(center)
    bm = bmesh.new()
    v0, e0 = ngon_ring(bm, (center.x - width / 2, center.y, center.z), radius, 20, axis="X")
    cap0 = bm.faces.new(v0[::-1])
    v1, e1 = extrude_ring(bm, e0, translate=(width, 0, 0))
    cap1 = bm.faces.new(v1)
    if groove:
        bm.faces.ensure_lookup_table()
        side, seen = [], set()
        for v in list(v0) + list(v1):
            for f in v.link_faces:
                if f in seen:
                    continue
                seen.add(f)
                c = f.calc_center_median()
                if abs(c.x - center.x) < width * 0.45:
                    side.append(f)

        def _angle(f):
            c = f.calc_center_median()
            return _atan2(c.z - center.z, c.y - center.y)

        side.sort(key=_angle)
        # notch()'s `inset` is an ABSOLUTE distance (inset_individual's thickness),
        # not a fraction of the face. A flat 0.10 exceeded the whole face on any
        # tyre narrower than 200 mm, so inset_individual inverted and the grooves
        # came out as lugs standing PROUD of the tread — which is what made every
        # bicycle look like a fatbike. Size both insets off the face itself.
        tread_arc = 2 * _PI * radius / 20.0          # circumferential face length
        tread_inset = min(width, tread_arc) * 0.22
        groove_depth = min(radius * 0.05, width * 0.35)
        for i, f in enumerate(side):
            if not f.is_valid:
                continue
            near_bottom = abs(_angle(f) - (-1.5708)) < 0.35   # contact-patch arc, stays full radius
            if i % 2 == 1 and not near_bottom:
                try:
                    notch(bm, f, inset=tread_inset, depth=groove_depth)
                except Exception:
                    pass
    if end_notch:
        for cap in (cap0, cap1):
            if cap.is_valid:
                try:
                    notch(bm, cap, inset=radius * 0.35, depth=radius * 0.04)
                except Exception:
                    pass
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(obj_name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(obj_name, me)
    _into(o)
    colour(o, rgb)
    return o

def make_wheel(name, loc, r, w, rim=0.45):
    """A wheel as ONE object, origin at the axle centre, axle along X (so rotating
    it about its local axis spins it). A treaded tyre (real grooves, not a plain
    cylinder) with an optional spoked chrome hub joined into one mesh."""
    tire = _wheel_ring(name, loc, r, w, (0.05, 0.05, 0.05), groove=True, end_notch=False)
    if rim > 0.01:
        hub = _wheel_ring(name + "_hub", loc, r * rim, w * 1.08,
                          (0.55, 0.55, 0.58), groove=False, end_notch=True)
        bpy.ops.object.select_all(action="DESELECT")
        hub.select_set(True); tire.select_set(True)
        bpy.context.view_layer.objects.active = tire
        bpy.ops.object.join()          # merge hub into tyre; keeps the tyre's axle origin
    tire["role"] = "wheel"             # role tag survives name-collision renames
    tire["wheel_label"] = name
    return tire

def tube_between(a, b, r, rgb):
    """A cylinder spanning points a->b. UNUSED by every base since bike's AAA pass
    moved its frame/fork tubes to loft(), which takes the same a->b span plus a
    radius per station and welds a real mesh instead of adding a primitive. Kept as
    the only primitive_cylinder_add left in the toolkit; reach for loft() instead."""
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

# Ring-loft mesh toolkit (the quality upgrade — see the *.md cheat-sheets). Instead
# of stacking separately-scaled primitives, organic/tapered shapes are LOFTED from a
# spine of (centre, radius) sections: a ring of verts is built at each section,
# oriented perpendicular to the local tangent, and consecutive rings are bridged into
# smooth quads. Verts are emitted in WORLD coordinates with the object left at the
# origin, so proximity-based bone weighting can read v.co directly (matches the doc).
#   loft        — tapered tube/limb/torso from sections
#   uvsphere_bm — head / eyes as a real bmesh sphere
#   finish      — shade-smooth + optional Subsurf/Bevel (the silhouette chokepoint)
#   uv_unwrap   — smart-project UVs (Unity-ready), tolerant of headless context
#   collider_hull / make_lods — Unity COL_ hull + LOD0_/LOD1_ decimated copies
_MESH_TOOLKIT = '''
from math import pi as _PI, cos as _cos, sin as _sin

def loft(name, sections, rgb, n=12, caps=True):
    """Build a smooth tapered mesh from a spine of (center, radius) sections. Each
    ring is perpendicular to the local tangent; consecutive rings are bridged."""
    import bmesh, mathutils
    centers = [mathutils.Vector(c) for c, _ in sections]
    radii = [float(r) for _, r in sections]
    bm = bmesh.new()
    rings = []
    m = len(centers)
    for i, (c, r) in enumerate(zip(centers, radii)):
        if i == 0:
            t = centers[1] - centers[0]
        elif i == m - 1:
            t = centers[-1] - centers[-2]
        else:
            t = centers[i + 1] - centers[i - 1]
        if t.length < 1e-6:
            t = mathutils.Vector((0, 0, 1))
        t.normalize()
        up = mathutils.Vector((0, 0, 1))
        if abs(t.dot(up)) > 0.99:
            up = mathutils.Vector((1, 0, 0))
        u = t.cross(up).normalized()
        v = t.cross(u).normalized()
        ring = []
        for k in range(n):
            a = 2 * _PI * k / n
            ring.append(bm.verts.new(c + (u * _cos(a) + v * _sin(a)) * r))
        rings.append(ring)
    bm.verts.ensure_lookup_table()
    for i in range(m - 1):
        for k in range(n):
            try:
                bm.faces.new([rings[i][k], rings[i][(k + 1) % n],
                              rings[i + 1][(k + 1) % n], rings[i + 1][k]])
            except ValueError:
                pass
    if caps and m >= 1:
        try:
            bm.faces.new(rings[0][::-1])
        except ValueError:
            pass
        try:
            bm.faces.new(rings[-1])
        except ValueError:
            pass
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    _into(o)
    colour(o, rgb)
    return o

def uvsphere_bm(name, center, r, rgb, seg=16, ring=8):
    """A real (smooth) sphere via bmesh, centred at a world position."""
    import bmesh, mathutils
    bm = bmesh.new()
    try:
        bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=ring, radius=r)
    except TypeError:
        bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=ring, diameter=r * 2)
    bmesh.ops.translate(bm, vec=mathutils.Vector(center), verts=bm.verts)
    me = bpy.data.meshes.new(name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    _into(o)
    colour(o, rgb)
    return o

def finish(o, smooth=True, subsurf=0, bevel=0.0):
    """Smooth shading + optional Subsurf/Bevel — the single chokepoint that lifts a
    blocky loft into a clean low-poly silhouette. Operator-free (headless-safe)."""
    if smooth:
        for poly in o.data.polygons:
            poly.use_smooth = True
    if subsurf:
        m = o.modifiers.new("sub", "SUBSURF")
        m.levels = subsurf
        m.render_levels = subsurf
    if bevel:
        m = o.modifiers.new("bev", "BEVEL")
        m.width = bevel
        m.segments = 2
    return o

def uv_unwrap(o, angle_limit=1.15, island_margin=0.02):
    """Smart-project UVs so Unity gets a sane atlas. Tolerant: any failure (no
    context in headless, degenerate mesh) leaves the object usable, unwrapped."""
    try:
        bpy.ops.object.select_all(action="DESELECT")
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.uv.smart_project(angle_limit=angle_limit, island_margin=island_margin)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as _e:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    return o

def collider_hull(name, parts):
    """One convex-hull mesh (COL_<name>) merged from all render parts — Unity wires
    it to a convex MeshCollider. Modifier-free and hidden from render."""
    import bmesh
    src = [o for o in parts if o.type == "MESH" and o.get("role") != "collider"]
    if not src:
        return None
    dups = []
    for o in src:
        d = o.copy()
        d.data = o.data.copy()
        d.modifiers.clear()
        d.parent = None
        _into(d)
        dups.append(d)
    bpy.ops.object.select_all(action="DESELECT")
    for d in dups:
        d.select_set(True)
    bpy.context.view_layer.objects.active = dups[0]
    if len(dups) > 1:
        bpy.ops.object.join()
    col = bpy.context.active_object
    col.name = "COL_" + name
    me = col.data
    bm = bmesh.new()
    bm.from_mesh(me)
    try:
        res = bmesh.ops.convex_hull(bm, input=bm.verts[:])
        interior = res.get("geom_interior", [])
        if interior:
            bmesh.ops.delete(bm, geom=interior, context="VERTS")
    except Exception:
        pass
    bm.to_mesh(me)
    bm.free()
    col.hide_render = True
    col["role"] = "collider"
    return col

def make_lods(parts, levels=2):
    """Rename render parts LOD0_* and emit decimated LOD1_* (… up to `levels`)
    copies. Skinned copies keep their armature modifier + vertex groups, so the FBX
    exporter bakes the decimate but preserves the skin. Returns the new LOD objects."""
    meshes = [o for o in parts if o.type == "MESH" and o.get("role") != "collider"]
    made = []
    for o in meshes:
        if not o.name.startswith("LOD0_"):
            o.name = "LOD0_" + o.name
    for i in range(1, max(1, levels)):
        for o in meshes:
            d = o.copy()
            d.data = o.data.copy()
            _into(d)
            d.name = o.name.replace("LOD0_", "LOD%d_" % i, 1)
            # drop subsurf on the LOD copy (subsurf-then-decimate is wasteful and
            # defeats the point); keep the armature modifier so the skin survives.
            for mod in [m for m in d.modifiers if m.type in ("SUBSURF", "BEVEL")]:
                d.modifiers.remove(mod)
            dm = d.modifiers.new("dec", "DECIMATE")
            dm.ratio = max(0.08, 1.0 / (2 ** i))
            d["role"] = "lod"
            made.append(d)
    return made

# Edge-loop sculpting toolkit (AAA pass — see docs/PROCEDURAL_BASES.md). Grows a
# part as ONE welded mesh from an open boundary ring, the same move as extruding
# an edge loop in the modeller: duplicate the ring, move/scale the copy, weld it
# back on. No part may fall back to a bare box()/cyl()/cone()/sphere() union.
#   quad_ring / ngon_ring — start an open boundary ring (rect or round cross-section)
#   extrude_ring          — the core move: duplicate -> transform -> bridge_loops
#   bevel_edges           — crisp hard-surface edges on a specific edge set
#   notch                 — recess a panel into a face (openings, texture detail)
def quad_ring(bm, center, w, h, axis="Y"):
    """4 verts forming a rectangular cross-section ring perpendicular to `axis`,
    centred at `center`. axis='Y' (default) -> the ring lies in the XZ plane, i.e.
    forward is +Y (this file's convention for cars/planes/weapons)."""
    cx, cy, cz = center
    if axis == "Y":
        pts = [(cx - w/2, cy, cz - h/2), (cx + w/2, cy, cz - h/2),
               (cx + w/2, cy, cz + h/2), (cx - w/2, cy, cz + h/2)]
    elif axis == "X":
        pts = [(cx, cy - w/2, cz - h/2), (cx, cy + w/2, cz - h/2),
               (cx, cy + w/2, cz + h/2), (cx, cy - w/2, cz + h/2)]
    else:  # 'Z'
        pts = [(cx - w/2, cy - h/2, cz), (cx + w/2, cy - h/2, cz),
               (cx + w/2, cy + h/2, cz), (cx - w/2, cy + h/2, cz)]
    verts = [bm.verts.new(p) for p in pts]
    edges = [bm.edges.new((verts[i], verts[(i + 1) % 4])) for i in range(4)]
    return verts, edges

def ngon_ring(bm, center, radius, n, axis="Y"):
    """n verts around a circle perpendicular to `axis`, centred at `center` — the
    round-cross-section counterpart to quad_ring (barrels, scope tubes, spokes)."""
    cx, cy, cz = center
    verts = []
    for k in range(n):
        a = 2 * _PI * k / n
        if axis == "Y":
            p = (cx + radius * _cos(a), cy, cz + radius * _sin(a))
        elif axis == "X":
            p = (cx, cy + radius * _cos(a), cz + radius * _sin(a))
        else:
            p = (cx + radius * _cos(a), cy + radius * _sin(a), cz)
        verts.append(bm.verts.new(p))
    edges = [bm.edges.new((verts[i], verts[(i + 1) % n])) for i in range(n)]
    return verts, edges

def extrude_ring(bm, edges, translate=(0.0, 0.0, 0.0), scale=1.0):
    """Duplicate an open boundary edge loop, translate/scale the copy, then weld
    it back onto the body with bridge_loops — grows the mesh by one section as
    ONE continuous surface, never a union. `scale` is a float (uniform) or an
    (sx, sy, sz) tuple, applied about the loop's own centroid. `edges` must be an
    equal-length ring (any n, quad or n-gon) for bridge_loops to match them up.
    Returns (new_verts, new_edges) so the caller keeps extruding forward."""
    import bmesh, mathutils
    dup = bmesh.ops.duplicate(bm, geom=list(edges))
    geom = dup["geom"]
    new_verts = [g for g in geom if isinstance(g, bmesh.types.BMVert)]
    new_edges = [g for g in geom if isinstance(g, bmesh.types.BMEdge)]
    if scale != 1.0:
        vec = scale if isinstance(scale, (tuple, list)) else (scale, scale, scale)
        center = sum((v.co for v in new_verts), mathutils.Vector()) / len(new_verts)
        bmesh.ops.scale(bm, verts=new_verts, vec=mathutils.Vector(vec),
                        space=mathutils.Matrix.Translation(-center))
    if translate != (0.0, 0.0, 0.0):
        bmesh.ops.translate(bm, verts=new_verts, vec=mathutils.Vector(translate))
    bmesh.ops.bridge_loops(bm, edges=list(edges) + new_edges)
    return new_verts, new_edges

def bevel_edges(bm, edges, width, segments=2):
    """Bevel a specific edge set (not a blanket modifier) — muzzle crowns, receiver
    corners, a trigger-guard opening. Local and selective, unlike finish()'s
    whole-object BEVEL modifier."""
    import bmesh
    bmesh.ops.bevel(bm, geom=list(edges), offset=width, segments=segments,
                    affect="EDGES")

def notch(bm, face, inset=0.15, depth=0.02):
    """Recess a panel into `face`: inset its border, then push the inner face
    inward along the normal — a trigger-guard opening, grip texture panel, mag
    seam. inset_individual's return is the BORDER ring faces, not the shrunk
    inner one, so the inner face is found by geometry (closest surviving face
    with a matching normal) rather than trusted from the op's result. Built
    from the same duplicate/bridge primitive as extrude_ring, not
    extrude_face_region, which left the recess with an open (non-manifold)
    boundary on a single-face extrude. Returns True."""
    import bmesh
    center = face.calc_center_median()
    normal = face.normal.copy()
    bmesh.ops.inset_individual(bm, faces=[face], thickness=inset, depth=0.0)
    bm.faces.ensure_lookup_table()
    candidates = [f for f in bm.faces if f.normal.dot(normal) > 0.9]
    inner = min(candidates, key=lambda f: (f.calc_center_median() - center).length)
    inner_edges = list(inner.edges)
    bmesh.ops.delete(bm, geom=[inner], context="FACES_ONLY")
    dup = bmesh.ops.duplicate(bm, geom=inner_edges)
    new_verts = [g for g in dup["geom"] if isinstance(g, bmesh.types.BMVert)]
    new_edges = [g for g in dup["geom"] if isinstance(g, bmesh.types.BMEdge)]
    bmesh.ops.translate(bm, verts=new_verts, vec=-normal * depth)
    bmesh.ops.bridge_loops(bm, edges=inner_edges + new_edges)
    bmesh.ops.edgeloop_fill(bm, edges=new_edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    return True
'''

_PLANE_SRC = '''
def build(p, ox):
    """Recognisable propeller aircraft: a ring-loft fuselage (tapered tube, not an
    ellipsoid) with a sculpted nose/tail cap and a domed cockpit canopy, tapered
    aerofoil wings/stabilisers, ring-loft nacelle engines with tapered 3-blade
    propellers, and a tricycle landing gear (tapered struts + treaded wheels) that
    parks the wheels on the ground (z~0). Every part is edge-loop sculpted
    (quad_ring/ngon_ring + extrude_ring) or, for the canopy, a real bmesh sphere
    (uvsphere_bm) — never a bare box()/cyl()/cone()/sphere() union. Forward is -Y
    (so it matches the other bases and imports nose-forward in Unity). Each part is
    tagged with its mechanical bone so the rigid rigger + VehicleBoneDriver spin the
    props (`Propeller_*`) and roll/steer the wheels (`Wheel.*`). Knobs: fuselage_len/_r,
    wing_span, wing_chord, fin_h."""
    import bmesh, math, mathutils
    fl = p["fuselage_len"]; fr = p["fuselage_r"]
    span = p["wing_span"]; chord = p["wing_chord"]; fin = p["fin_h"]
    body = tuple(p["color"]); wingc = tuple(min(1.0, c * 0.92) for c in p["color"])
    glass = (0.40, 0.60, 0.90); metal = (0.30, 0.30, 0.30); accent = (0.80, 0.20, 0.20)
    dark = (0.10, 0.10, 0.10); strutc = (0.50, 0.50, 0.50)
    parts = []
    wr = max(0.10, fr * 0.30)          # gear wheel radius
    gl = max(0.22, fr * 1.10)          # gear strut length
    cz = wr + gl + fr * 0.55           # fuselage centreline -> wheels rest near z=0
    fwd = -1.0                          # nose direction (-Y)
    eng_x = span * 0.30
    hub_dy = fl * 0.05 + fl * 0.17 + fr * 0.2

    def tag(o, bone, role="plane"):
        o["bone"] = bone; o["role"] = role; parts.append(o); return o

    def pobj(bm, name, rgb, bevel=0.004):
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        me = bpy.data.meshes.new(name + "_m")
        bm.to_mesh(me); bm.free()
        o = bpy.data.objects.new(name, me)
        _into(o); colour(o, rgb); finish(o, smooth=True, subsurf=0, bevel=bevel)
        return o

    def cone_cap(name, base_center, tip_center, r0, rgb):
        """A short tapered cone (base ring -> point) — nose cap / tail cone."""
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, base_center, r0, 12, axis="Y")
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=tuple(t - b for t, b in zip(tip_center, base_center)),
                              scale=0.02)
        bm.faces.new(v1)
        return pobj(bm, name, rgb)

    def aerofoil(name, root_center, axis_vec, span_len, chord_w, thick_h, ring_axis, rgb, taper=0.4):
        """A wing/stabiliser/blade: full-section root -> tapered tip, extruded along
        `axis_vec` (a unit-ish direction with the right sign) by `span_len`."""
        bm = bmesh.new()
        if ring_axis == "X":
            v0, e0 = quad_ring(bm, root_center, chord_w, thick_h, axis="X")
        else:
            v0, e0 = quad_ring(bm, root_center, chord_w, thick_h, axis="Z")
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=tuple(c * span_len for c in axis_vec),
                              scale=(1, taper, taper) if ring_axis == "X" else (taper, taper, 1))
        bm.faces.new(v1)
        return pobj(bm, name, rgb, bevel=0.003)

    # ── fuselage: ring-loft tube, tapered at both ends ──
    bm = bmesh.new()
    r0 = fr * 0.32
    v0, e0 = ngon_ring(bm, (ox, -fl * 0.5, cz), r0, 14, axis="Y")
    bm.faces.new(v0[::-1])
    v1, e1 = extrude_ring(bm, e0, translate=(0, fl * 0.16, 0), scale=(fr / r0, 1, fr / r0))
    v2, e2 = extrude_ring(bm, e1, translate=(0, fl * 0.34, 0))
    v3, e3 = extrude_ring(bm, e2, translate=(0, fl * 0.34, 0))
    v4, e4 = extrude_ring(bm, e3, translate=(0, fl * 0.16, 0), scale=(r0 / fr, 1, r0 / fr))
    bm.faces.new(v4)
    tag(pobj(bm, "Fuselage", body), "Fuselage")

    # nose cap (beyond the fuselage's forward taper) + tail cone (beyond the rear)
    tag(cone_cap("Nose", (ox, fwd * fl * 0.5, cz), (ox, fwd * (fl * 0.5 + fr * 0.55), cz),
                fr * 0.34, body), "Fuselage")
    tag(cone_cap("TailCone", (ox, -fwd * fl * 0.5, cz), (ox, -fwd * (fl * 0.5 + fr * 0.42), cz),
                fr * 0.34, body), "Fuselage")

    # cockpit canopy: a real bmesh dome, not a bare primitive
    cockpit = uvsphere_bm("Cockpit", (ox, fwd * (fl * 0.12), cz + fr * 0.35), fr * 0.62, glass,
                          seg=14, ring=8)
    cockpit.scale = (1.0, 1.5, 0.75)
    bpy.context.view_layer.objects.active = cockpit
    bpy.ops.object.select_all(action="DESELECT"); cockpit.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    tag(cockpit, "Fuselage")

    # wings (span along X), mounted low, tapered root->tip
    wt = max(0.04, fr * 0.16)
    for sgn, nm in ((1, "Wing_R"), (-1, "Wing_L")):
        tag(aerofoil(nm, (ox, fwd * (-fl * 0.02), cz - fr * 0.25), (sgn, 0, 0), span * 0.54,
                    chord, wt, "X", wingc), "Fuselage")

    # tail surfaces at the +Y end
    tail_y = -fwd * (fl * 0.46)
    tag(aerofoil("V_Stabilizer", (ox, tail_y, cz), (0, 0, 1), fin,
                0.06 + fr * 0.1, chord * 0.6, "Z", accent), "Fuselage")
    for sgn, nm in ((1, "H_Stabilizer_R"), (-1, "H_Stabilizer_L")):
        tag(aerofoil(nm, (ox, tail_y, cz), (sgn, 0, 0), span * 0.26,
                    chord * 0.5, wt * 0.8, "X", accent), "Fuselage")

    # engines under the wings (ring-loft nacelle) + 3-blade tapered propellers
    for sgn, suf in ((1, "R"), (-1, "L")):
        ex = ox + sgn * eng_x
        ey = cz - fr * 0.45
        er = fr * 0.42
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, (ex, fwd * (fl * 0.05 - fl * 0.17), ey), er * 0.55, 12, axis="Y")
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=(0, fwd * fl * 0.10, 0), scale=(er / (er * 0.55), 1, er / (er * 0.55)))
        v2, e2 = extrude_ring(bm, e1, translate=(0, fwd * fl * 0.14, 0))
        v3, e3 = extrude_ring(bm, e2, translate=(0, fwd * fl * 0.10, 0), scale=0.8)
        bm.faces.new(v3)
        tag(pobj(bm, "Engine_" + suf, metal), "Fuselage")

        hub_y = fwd * hub_dy
        tag(cone_cap("PropHub_" + suf, (ex, hub_y - fwd * fr * 0.05, ey),
                    (ex, hub_y + fwd * fr * 0.45, ey), fr * 0.20, metal),
            "Propeller_" + suf, "prop")
        for i in range(3):
            # rotate the BMESH verts around the hub before conversion — every part in
            # this file keeps its verts in world coords with the object left at the
            # origin (matches loft()'s convention), so rotating the finished OBJECT
            # afterwards would pivot around world (0,0,0), not the hub, and fling the
            # blade away (caught by the headless render check, not the compile check).
            bm = bmesh.new()
            v0, e0 = quad_ring(bm, (ex, hub_y, ey), 0.04 + fr * 0.05, 0.02, axis="Z")
            bm.faces.new(v0[::-1])
            v1, e1 = extrude_ring(bm, e0, translate=(0, 0, fr * 1.7), scale=(1, 0.15, 0.15))
            bm.faces.new(v1)
            bmesh.ops.rotate(bm, cent=(ex, hub_y, ey),
                             matrix=mathutils.Matrix.Rotation(math.radians(i * 120), 3, "Y"),
                             verts=bm.verts[:])
            bl = pobj(bm, "PropBlade_%d_%s" % (i, suf), dark, bevel=0.002)
            tag(bl, "Propeller_" + suf, "prop")

    # tricycle landing gear: tapered strut (-> Fuselage) + rolling wheel (-> Wheel.*)
    def gear(gx, gy, wheel_bone):
        top = (ox + gx, gy, cz - fr * 0.5)
        bot = (ox + gx, gy, cz - fr * 0.5 - gl)
        bm = bmesh.new()
        r0 = fr * 0.05; joint_r = fr * 0.075
        v0, e0 = ngon_ring(bm, top, r0, 8, axis="Z")
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=(0, 0, -gl * 0.5), scale=(joint_r / r0, joint_r / r0, 1))
        v2, e2 = extrude_ring(bm, e1, translate=(0, 0, -gl * 0.5), scale=(r0 / joint_r, r0 / joint_r, 1))
        bm.faces.new(v2)
        st = pobj(bm, "Strut_" + wheel_bone.replace("Wheel.", ""), strutc)
        tag(st, "Fuselage")
        tag(make_wheel(wheel_bone, (ox + gx, gy, wr), wr, wr * 0.5, 0.4), wheel_bone, "wheel")
    gear(0.0,            fwd * (fl * 0.33),  "Wheel.F")
    gear(span * 0.10,  -fwd * (fl * 0.02),  "Wheel.RR")
    gear(-span * 0.10, -fwd * (fl * 0.02),  "Wheel.RL")

    # bone table for the rigid mechanical rigger (single source of truth)
    global PLANE_BONES
    PLANE_BONES = [
        ("Root",     (ox, 0, cz), (ox, 0, cz + 0.5), None),
        ("Fuselage", (ox, 0, cz), (ox, fwd * (fl * 0.3), cz), "Root"),
    ]
    for sgn, suf in ((1, "R"), (-1, "L")):
        ex = ox + sgn * eng_x; hub_y = fwd * hub_dy
        PLANE_BONES.append(("Propeller_" + suf, (ex, hub_y, cz - fr * 0.45),
                            (ex, hub_y + fwd * 0.3, cz - fr * 0.45), "Fuselage"))
    for nm, gx, gy in (("Wheel.F", 0.0, fwd * (fl * 0.33)),
                       ("Wheel.RR", span * 0.10, -fwd * (fl * 0.02)),
                       ("Wheel.RL", -span * 0.10, -fwd * (fl * 0.02))):
        PLANE_BONES.append((nm, (ox + gx, gy, wr), (ox + gx + 0.3, gy, wr), "Fuselage"))
    return parts
'''

_HUMANOID_SRC = '''
def build(p, ox):
    """A-pose humanoid built from the proven humangen.py recipe: stacked vertex-ring
    tubes per body part (arms down at the sides, real bmesh head/eyes), proportions
    that read as human by construction. Ported into the parametric framework — scaled
    to `height`, coloured from the palette, feet shifted to the floor (z=0), and every
    part tagged with its Unity HumanBodyBones name so the rigger skins a valid Humanoid
    avatar. Verts are in WORLD coords (object at origin) so weighting reads v.co.
    Reference proportions span z=-0.4..1.8 (~2.2 tall); `rfac` thins radii for lean."""
    import bmesh, math
    h = p["height"]
    pal = p.get("palette") or {}
    skin  = tuple(pal.get("skin",  p["color"]))
    shirt = tuple(pal.get("shirt", (0.20, 0.40, 0.80)))
    pants = tuple(pal.get("pants", (0.15, 0.15, 0.25)))
    shoe  = tuple(pal.get("shoe",  (0.10, 0.10, 0.10)))
    hair  = tuple(pal.get("hair",  (0.12, 0.08, 0.05)))
    eyec  = tuple(pal.get("eye",   (0.92, 0.92, 0.94)))
    pupil = tuple(pal.get("pupil", (0.05, 0.05, 0.05)))
    s = h / 2.2
    rfac = max(0.6, min(1.4, p["limb_r"] / 0.08))

    def X(x): return ox + x * s
    def Y(y): return y * s
    def Z(z): return (z + 0.4) * s          # shift so the lowest point (toe) sits at z=0
    def R(r): return r * s * rfac
    parts = []

    def stack(name, secs, rgb, part, bone, nseg=10):
        """A tapered tube from a list of (cx, cy, cz, r) rings (reference coords),
        bridged into smooth quads + end caps. One named, role/part/bone-tagged obj."""
        bm = bmesh.new()
        rings = []
        for (cx, cy, cz, r) in secs:
            ring = []
            for i in range(nseg):
                a = 2 * math.pi * i / nseg
                ring.append(bm.verts.new((X(cx) + R(r) * math.cos(a),
                                          Y(cy) + R(r) * math.sin(a), Z(cz))))
            rings.append(ring)
        for ri in range(len(rings) - 1):
            for i in range(nseg):
                try:
                    bm.faces.new([rings[ri][i], rings[ri][(i + 1) % nseg],
                                  rings[ri + 1][(i + 1) % nseg], rings[ri + 1][i]])
                except ValueError:
                    pass
        try:
            bm.faces.new(rings[0][::-1])
        except ValueError:
            pass
        try:
            bm.faces.new(rings[-1])
        except ValueError:
            pass
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        me = bpy.data.meshes.new(name + "_m")
        bm.to_mesh(me)
        bm.free()
        o = bpy.data.objects.new(name, me)
        _into(o)
        colour(o, rgb)
        finish(o)            # shade-smooth only: subsurf shrinks open tubes -> gaps
        o["role"] = "human"; o["part"] = part; o["bone"] = bone
        parts.append(o)
        return o

    def ball(name, c, r, rgb, part, bone, seg=16, ring=10):
        o = uvsphere_bm(name, (X(c[0]), Y(c[1]), Z(c[2])), R(r), rgb, seg=seg, ring=ring)
        finish(o)
        o["role"] = "human"; o["part"] = part; o["bone"] = bone
        parts.append(o)
        return o

    # torso (5 rings hips->shoulders) + neck + head + hair cap
    stack("Torso", [(0, 0, 1.4, 0.18), (0, 0, 1.2, 0.20), (0, 0, 0.95, 0.22),
                    (0, 0, 0.7, 0.20), (0, 0, 0.5, 0.18)], shirt, "Torso", "Spine", nseg=12)
    stack("Neck", [(0, 0, 1.55, 0.07), (0, 0, 1.4, 0.08)], skin, "Neck", "Neck")
    ball("Head", (0, 0, 1.65), 0.12, skin, "Head", "Head")
    ball("Hair", (0, 0, 1.66), 0.135, hair, "Hair", "Head", seg=14, ring=9)
    ball("Eye_L", (0.06, -0.06, 1.65), 0.025, eyec, "Eye", "Head", seg=8, ring=6)
    ball("Eye_R", (-0.06, -0.06, 1.65), 0.025, eyec, "Eye", "Head", seg=8, ring=6)
    ball("Pupil_L", (0.06, -0.08, 1.65), 0.012, pupil, "Pupil", "Head", seg=6, ring=4)
    ball("Pupil_R", (-0.06, -0.08, 1.65), 0.012, pupil, "Pupil", "Head", seg=6, ring=4)

    # arms (down at the sides) + legs + feet/toes, both sides
    for sgn, pre in ((1, "Left"), (-1, "Right")):
        ax = 0.2 * sgn; lx = 0.12 * sgn
        stack(pre + "UpperArm", [(ax, 0, 1.35, 0.06), (ax, 0, 1.1, 0.055)],
              skin, pre + "UpperArm", pre + "UpperArm")
        stack(pre + "LowerArm", [(ax, 0, 1.1, 0.055), (ax, 0, 0.8, 0.045)],
              skin, pre + "LowerArm", pre + "LowerArm")
        stack(pre + "Hand", [(ax, 0, 0.8, 0.04), (ax, 0, 0.65, 0.035)],
              skin, pre + "Hand", pre + "Hand", nseg=8)
        stack(pre + "UpperLeg", [(lx, 0, 0.5, 0.08), (lx, 0, 0.1, 0.065)],
              pants, pre + "UpperLeg", pre + "UpperLeg")
        stack(pre + "LowerLeg", [(lx, 0, 0.1, 0.065), (lx, 0, -0.35, 0.05)],
              pants, pre + "LowerLeg", pre + "LowerLeg")
        stack(pre + "Foot", [(lx, 0, -0.35, 0.05), (lx, 0.15, -0.4, 0.05)],
              shoe, pre + "Foot", pre + "Foot", nseg=8)
        stack(pre + "Toes", [(lx, 0.15, -0.4, 0.04), (lx, 0.25, -0.4, 0.03)],
              shoe, pre + "Toes", pre + "Foot", nseg=8)

    # bone table (Unity HumanBodyBones names) in WORLD coords + per-part weight map,
    # shared with rig_model via module globals (single source of truth).
    global BONES_WORLD, PART_BONES, WEIGHT_RADIUS
    def B(x, y, z): return (X(x), Y(y), Z(z))
    BONES_WORLD = [
        ("Hips",       B(0, 0, 0.5),  B(0, 0, 0.7),  None),
        ("Spine",      B(0, 0, 0.7),  B(0, 0, 0.95), "Hips"),
        ("Chest",      B(0, 0, 0.95), B(0, 0, 1.2),  "Spine"),
        ("UpperChest", B(0, 0, 1.2),  B(0, 0, 1.4),  "Chest"),
        ("Neck",       B(0, 0, 1.4),  B(0, 0, 1.55), "UpperChest"),
        ("Head",       B(0, 0, 1.55), B(0, 0, 1.8),  "Neck"),
    ]
    PART_BONES = {
        "Torso": {"Hips": 0.25, "Spine": 0.25, "Chest": 0.25, "UpperChest": 0.25},
        "Neck":  {"Neck": 0.7, "Head": 0.15, "UpperChest": 0.15},
        "Head":  {"Head": 0.9, "Neck": 0.1},
        "Hair":  {"Head": 1.0}, "Eye": {"Head": 1.0}, "Pupil": {"Head": 1.0},
    }
    for sgn, pre in ((1, "Left"), (-1, "Right")):
        ax = 0.2 * sgn; lx = 0.12 * sgn
        BONES_WORLD += [
            (pre + "UpperArm", B(ax, 0, 1.35), B(ax, 0, 1.1),   "UpperChest"),
            (pre + "LowerArm", B(ax, 0, 1.1),  B(ax, 0, 0.8),   pre + "UpperArm"),
            (pre + "Hand",     B(ax, 0, 0.8),  B(ax, 0, 0.65),  pre + "LowerArm"),
            (pre + "UpperLeg", B(lx, 0, 0.5),  B(lx, 0, 0.1),   "Hips"),
            (pre + "LowerLeg", B(lx, 0, 0.1),  B(lx, 0, -0.35), pre + "UpperLeg"),
            (pre + "Foot",     B(lx, 0, -0.35), B(lx, 0.15, -0.4), pre + "LowerLeg"),
            (pre + "Toes",     B(lx, 0.15, -0.4), B(lx, 0.25, -0.4), pre + "Foot"),
        ]
        PART_BONES[pre + "UpperArm"] = {pre + "UpperArm": 0.85, "UpperChest": 0.15}
        PART_BONES[pre + "LowerArm"] = {pre + "LowerArm": 0.85, pre + "UpperArm": 0.15}
        PART_BONES[pre + "Hand"]     = {pre + "Hand": 0.8, pre + "LowerArm": 0.2}
        PART_BONES[pre + "UpperLeg"] = {pre + "UpperLeg": 0.85, "Hips": 0.15}
        PART_BONES[pre + "LowerLeg"] = {pre + "LowerLeg": 0.85, pre + "UpperLeg": 0.15}
        PART_BONES[pre + "Foot"]     = {pre + "Foot": 0.85, pre + "LowerLeg": 0.15}
        PART_BONES[pre + "Toes"]     = {pre + "Foot": 0.8, pre + "LowerLeg": 0.2}
    WEIGHT_RADIUS = 0.2 * s
    return parts
'''

_BIKE_SRC = '''
def _kobj(bm, name, rgb, role, bevel=0.003, subsurf=0):
    """Weld a bike-part bmesh into a named, coloured, finished object carrying the
    role the rigger binds by (frame / fork / handlebar / wheel) — every hard-surface
    bike part goes through here, none through box()/cyl()/cone()/sphere()."""
    import bmesh as _bmesh
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    _into(o)
    colour(o, rgb)
    finish(o, smooth=True, subsurf=subsurf, bevel=bevel)
    o["role"] = role
    return o

def _se_ring(bm, y, cz, hw, hh, m=12, e=3.2):
    """A closed m-vert superellipse ring in the XZ plane (so it grows along Y):
    |x/hw|^e + |z/hh|^e = 1. e=2 is an ellipse, e~3-4 a rounded rectangle — one
    knob spanning a saddle's rounded-square section and a fuel tank's soft
    teardrop, neither of which a circle or a box can express."""
    verts = []
    for k in range(m):
        a = 2.0 * _PI * k / m
        ca = _cos(a); sa = _sin(a)
        x = hw * (1.0 if ca >= 0 else -1.0) * abs(ca) ** (2.0 / e)
        z = hh * (1.0 if sa >= 0 else -1.0) * abs(sa) ** (2.0 / e)
        verts.append(bm.verts.new((x, y, cz + z)))
    edges = [bm.edges.new((verts[i], verts[(i + 1) % m])) for i in range(m)]
    return verts, edges

def build(p, ox):
    """One parametric two-wheeler, every part edge-loop sculpted or ring-lofted: a
    full diamond frame (head/top/down/seat tubes + PAIRED chain and seat stays),
    twin tapered fork blades on a crown, a swept handlebar with grips, a sculpted
    saddle and superellipse tank, and — for a motorbike — a finned cylinder barrel
    over a crankcase with a bent exhaust. The same knobs span a bicycle (pedals,
    cranks, chainring), a motorbike (engine + tank), a dirt bike (suspension forks)
    and a unicycle (wheels=1). Forward is +Y, wheel axles along X.

    Every part keeps the `role` tag the rigger binds by (wheel / fork / handlebar /
    frame) and bone POSITIONS now come from BIKE_BONES, built here from the same
    numbers that placed the geometry. The rigger used to read `w.location`, which is
    (0,0,0) for every bmesh part in this file (verts are emitted in world coords,
    see loft()) — so both wheel bones sat at the world origin and rolling them
    orbited the wheels around it instead of spinning them on their axles."""
    import bmesh, math, mathutils
    global BIKE_BONES
    wb = p["wheelbase"]; wr = p["wheel_r"]; ww = p["wheel_w"]; fh = p["frame_h"]
    hbw = p["handlebar_w"]; rim = p.get("rim", 0.55)
    n = max(1, min(2, int(round(p["wheels"]))))
    engine = p["engine"] >= 0.5; tank = p["tank"] >= 0.5
    pedals = p["pedals"] >= 0.5; susp = p["suspension"] >= 0.5
    col = tuple(p["color"]); metal = (0.30, 0.30, 0.32)
    chrome = (0.62, 0.63, 0.66); dark = (0.07, 0.07, 0.08)
    parts = []
    seat_z = wr + fh
    tube_r = max(0.016, wr * 0.075)

    def tag(o, role):
        o["role"] = role
        parts.append(o)
        return o

    def tube(name, pts, radii, rgb, role="frame", seg=10, bevel=0.0):
        """A frame tube / fork blade / post / bent pipe: ring-loft along an arbitrary
        spine, so a swept handlebar and a curved exhaust are the same call as a
        straight seat tube, and a butted tube is just a radius per station."""
        o = loft(name, list(zip(pts, radii)), rgb, n=seg)
        finish(o, smooth=True, bevel=bevel)
        return tag(o, role)

    def yloft(name, stations, rgb, role, m=12, e=3.2, bevel=0.002):
        """Sculpt a body from superellipse stations along Y: [(y, cz, hw, hh), ...].
        The saddle (narrow nose, wide dished tail) and the tank (teardrop with a
        knee scallop) are the same loft with different station tables."""
        bm = bmesh.new()
        rings = [_se_ring(bm, y, cz, hw, hh, m, e)[0] for y, cz, hw, hh in stations]
        for i in range(len(rings) - 1):
            for k in range(m):
                try:
                    bm.faces.new([rings[i][k], rings[i][(k + 1) % m],
                                  rings[i + 1][(k + 1) % m], rings[i + 1][k]])
                except ValueError:
                    pass
        for cap in (rings[0][::-1], rings[-1]):
            try:
                bm.faces.new(cap)
            except ValueError:
                pass
        return tag(_kobj(bm, name, rgb, role, bevel=bevel), role)

    def saddle(name, cy, cz, ln, hw):
        """Nose (+Y) narrow, tail (-Y) wide, dished across the middle and rolled
        under at the rim — the shape a rider sits in, not a slab."""
        t = max(0.018, ln * 0.10)
        return yloft(name, [
            (cy + ln * 0.50, cz - t * 0.15, hw * 0.16, t * 0.45),
            (cy + ln * 0.30, cz - t * 0.05, hw * 0.30, t * 0.62),
            (cy + ln * 0.02, cz, hw * 0.62, t * 0.72),
            (cy - ln * 0.28, cz + t * 0.04, hw * 0.98, t * 0.78),
            (cy - ln * 0.50, cz - t * 0.02, hw * 0.86, t * 0.60),
        ], col, "frame", m=14, e=2.6)

    # ── unicycle: one wheel, a turned post, a saddle, cranks on the axle ──
    if n == 1:
        tag(make_wheel("Wheel.C", (ox, 0, wr), wr, ww, rim), "wheel")
        tube("post", [(ox, 0, wr), (ox, 0, wr + fh * 0.45), (ox, 0, seat_z + 0.06)],
             [tube_r * 1.25, tube_r * 0.85, tube_r * 0.80], chrome, "frame", seg=10)
        saddle("seat", 0.0, seat_z + 0.13, max(0.20, wr * 0.70), max(0.07, wr * 0.20))
        if pedals:
            # cranks point fore and aft (the pose a unicycle rests in), each a real
            # arm out of the hub — pedals floating beside the axle read as detached
            cr = max(0.08, wr * 0.42)
            for sx in (1, -1):
                cx = ox + sx * (ww * 0.5 + tube_r * 1.6)
                bm = bmesh.new()
                v, e = quad_ring(bm, (cx, 0, wr), tube_r * 1.3, tube_r * 2.0, axis="Y")
                bm.faces.new(v)
                v, e = extrude_ring(bm, e, translate=(0, sx * cr * 0.55, 0), scale=(0.9, 1, 0.8))
                v, e = extrude_ring(bm, e, translate=(0, sx * cr * 0.45, 0), scale=(1, 1, 0.9))
                bm.faces.new(v[::-1])
                tag(_kobj(bm, "crank_%d" % (sx > 0), chrome, "frame"), "frame")
                bm = bmesh.new()
                v, e = quad_ring(bm, (cx + sx * tube_r * 1.8, sx * cr, wr),
                                 0.09, 0.05, axis="Z")
                bm.faces.new(v)
                v, e = extrude_ring(bm, e, translate=(0, 0, -0.020))
                bm.faces.new(v[::-1])
                tag(_kobj(bm, "pedal_%d" % (sx > 0), dark, "frame"), "frame")
        BIKE_BONES = [
            ("root", (ox, 0, 0.02), (ox, 0, 0.30), None),
            ("frame", (ox, 0, wr), (ox, -0.30, wr), "root"),
            ("Wheel.C", (ox, 0, wr), (ox + 0.30, 0, wr), "root"),
        ]
        return parts

    front_y = wb * 0.5; rear_y = -wb * 0.5
    # The steering stack is measured off the TYRE, not off frame_h: a fraction of
    # frame_h put the crown 0.15 m inside the front wheel at the default knobs, so
    # the fork and lower head tube were buried in the tyre and the bike looked
    # fork-less. Crown clears the tyre, head tube sits on the crown, bar above that.
    crown_z = wr * 2.0 + max(0.02, wr * 0.10)
    head_b = (ox, front_y - wb * 0.055, crown_z + tube_r * 1.2)   # bottom, raked back
    head_t = (ox, front_y - wb * 0.115,
              max(head_b[2] + tube_r * 3.0, wr + fh))             # top of the head tube
    bb = (ox, rear_y * 0.28, wr * 0.62)                     # bottom bracket
    seat_t = (ox, rear_y * 0.34, seat_z)
    stay_x = ww * 0.5 + tube_r * 1.4
    fork_x = ww * 0.5 + tube_r * 0.9

    tag(make_wheel("Wheel.F", (ox, front_y, wr), wr, ww, rim), "wheel")
    tag(make_wheel("Wheel.R", (ox, rear_y, wr), wr, ww, rim), "wheel")

    # ── fork: two blades from the axle ends to a crown, plus the steerer. A
    # suspension fork gets fat sliders low and thin stanchions high (the step is a
    # radius change at a station, not a second overlapping cylinder). ──
    crown = (ox, head_b[1], head_b[2] - fh * 0.06)
    for sx in (1, -1):
        ax = ox + sx * fork_x
        if susp:
            blade = ([(ax, front_y, wr), (ax, front_y + (crown[1] - front_y) * 0.45,
                                          wr + (crown[2] - wr) * 0.45),
                      (ax, front_y + (crown[1] - front_y) * 0.52,
                       wr + (crown[2] - wr) * 0.52), (crown[0] + sx * fork_x * 0.5,
                                                      crown[1], crown[2])],
                     [tube_r * 1.30, tube_r * 1.30, tube_r * 0.92, tube_r * 0.92])
        else:
            blade = ([(ax, front_y, wr), (ax, front_y + (crown[1] - front_y) * 0.5,
                                          wr + (crown[2] - wr) * 0.5),
                      (crown[0] + sx * fork_x * 0.5, crown[1], crown[2])],
                     [tube_r * 0.62, tube_r * 0.85, tube_r * 0.95])
        tube("fork_%d" % (sx > 0), blade[0], blade[1],
             (0.50, 0.50, 0.55) if susp else metal, "fork", seg=10)
    tube("fork_crown", [(ox - fork_x * 0.6, crown[1], crown[2]),
                        (ox + fork_x * 0.6, crown[1], crown[2])],
         [tube_r * 1.15, tube_r * 1.15], metal, "fork", seg=8)
    tube("steerer", [crown, head_t], [tube_r * 0.85, tube_r * 0.85], chrome, "fork", seg=10)

    # ── handlebar: a stem out of the steerer, then a swept bar (curved spine, so
    # the sweep is lofted, not faked) with fatter grips at the ends ──
    hz = head_t[2] + fh * 0.06
    tube("stem", [head_t, (ox, head_t[1] - wb * 0.045, hz)],
         [tube_r * 0.80, tube_r * 0.70], metal, "handlebar", seg=8)
    bar_y = head_t[1] - wb * 0.045
    sweep = wb * 0.055
    bar_pts = [(ox - hbw * 0.5, bar_y - sweep, hz - fh * 0.03),
               (ox - hbw * 0.30, bar_y - sweep * 0.40, hz),
               (ox, bar_y, hz),
               (ox + hbw * 0.30, bar_y - sweep * 0.40, hz),
               (ox + hbw * 0.5, bar_y - sweep, hz - fh * 0.03)]
    tube("handlebar", bar_pts,
         [tube_r * 0.52, tube_r * 0.58, tube_r * 0.62, tube_r * 0.58, tube_r * 0.52],
         metal, "handlebar", seg=10)
    for sx in (1, -1):
        gx = ox + sx * hbw * 0.5
        tube("grip_%d" % (sx > 0),
             [(gx - sx * hbw * 0.14, bar_y - sweep * 0.72, hz - fh * 0.015),
              (gx, bar_y - sweep, hz - fh * 0.03)],
             [tube_r * 0.66, tube_r * 0.62], dark, "handlebar", seg=10)

    # ── frame: a real diamond. head + top + down + seat tubes, then PAIRED chain
    # and seat stays either side of the wheel (one centreline stay read as a bike
    # sliced in half from behind). ──
    for name, a, b, ra, rb in (
        ("head_tube", head_b, head_t, tube_r * 1.25, tube_r * 1.25),
        ("top_tube", head_t, (seat_t[0], seat_t[1], seat_t[2] - tube_r), tube_r, tube_r * 0.92),
        ("down_tube", head_b, bb, tube_r * 1.15, tube_r * 1.05),
        ("seat_tube", bb, seat_t, tube_r * 1.05, tube_r * 0.95),
    ):
        tube(name, [a, b], [ra, rb], col, "frame", seg=10)
    for sx in (1, -1):
        rear_hub = (ox + sx * stay_x, rear_y, wr)
        tube("chain_stay_%d" % (sx > 0), [(bb[0] + sx * stay_x * 0.5, bb[1], bb[2]), rear_hub],
             [tube_r * 0.72, tube_r * 0.55], col, "frame", seg=8)
        tube("seat_stay_%d" % (sx > 0),
             [(seat_t[0] + sx * stay_x * 0.35, seat_t[1], seat_t[2] - tube_r), rear_hub],
             [tube_r * 0.62, tube_r * 0.48], col, "frame", seg=8)

    tube("seat_post", [(seat_t[0], seat_t[1], seat_t[2] - tube_r * 0.5),
                       (seat_t[0], seat_t[1], seat_z + fh * 0.10)],
         [tube_r * 0.80, tube_r * 0.78], chrome, "frame", seg=8)
    saddle("seat", seat_t[1], seat_z + fh * 0.16, max(0.20, wb * 0.22), max(0.07, wb * 0.062))

    # ── engine: crankcase + a FINNED cylinder barrel (alternating extrusions, the
    # detail that makes an engine read as an engine) + a bent exhaust ──
    if engine:
        ew = max(0.14, wb * 0.20); el = max(0.16, wb * 0.26); eh = max(0.14, wr * 0.52)
        bm = bmesh.new()
        v, e = quad_ring(bm, (ox, bb[1], bb[2] - eh * 0.10), ew, el, axis="Z")
        bm.faces.new(v)
        v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.30), scale=(1.06, 1.02, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.42), scale=(0.94, 0.96, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.18), scale=(0.80, 0.86, 1))
        bm.faces.new(v[::-1])
        bm.faces.ensure_lookup_table()
        side = max(bm.faces, key=lambda f: f.calc_center_median().x)
        try:
            notch(bm, side, inset=min(ew, eh) * 0.22, depth=eh * 0.03)   # case cover
        except Exception:
            pass
        tag(_kobj(bm, "engine", metal, "frame", bevel=0.004), "frame")
        cyl_z = bb[2] + eh * 0.80
        bm = bmesh.new()
        v, e = ngon_ring(bm, (ox, bb[1] + el * 0.10, cyl_z), ew * 0.34, 12, axis="Z")
        bm.faces.new(v)
        for i in range(5):                       # cooling fins: out, up, back in
            v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.055), scale=1.42)
            v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.030))
            v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.025), scale=1 / 1.42)
        v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.10), scale=1.30)   # head
        v, e = extrude_ring(bm, e, translate=(0, 0, eh * 0.06), scale=0.92)
        bm.faces.new(v[::-1])
        tag(_kobj(bm, "cylinder", (0.42, 0.43, 0.45), "frame", bevel=0.002), "frame")
        ex_x = ox + ew * 0.42
        tube("exhaust", [(ox + ew * 0.10, bb[1] + el * 0.42, cyl_z - eh * 0.10),
                         (ex_x, bb[1] + el * 0.20, bb[2] - eh * 0.20),
                         (ex_x, bb[1] - el * 0.60, bb[2] - eh * 0.05),
                         (ex_x, rear_y * 0.62, bb[2] + eh * 0.28),
                         (ex_x, rear_y * 0.88, bb[2] + eh * 0.42)],
             [tube_r * 0.55, tube_r * 0.60, tube_r * 0.62, tube_r * 0.95, tube_r * 0.88],
             chrome, "frame", seg=10)

    # ── fuel tank: superellipse teardrop straddling the top tube ──
    if tank:
        ty = (bb[1] + head_t[1]) * 0.5
        tl = max(0.22, wb * 0.42); tw = max(0.09, wb * 0.115); th = max(0.08, fh * 0.30)
        tz = seat_z - th * 0.55
        yloft("tank", [
            (ty + tl * 0.50, tz - th * 0.18, tw * 0.42, th * 0.42),
            (ty + tl * 0.22, tz, tw * 0.94, th * 0.86),
            (ty - tl * 0.10, tz + th * 0.04, tw, th),
            (ty - tl * 0.34, tz - th * 0.02, tw * 0.80, th * 0.80),
            (ty - tl * 0.50, tz - th * 0.20, tw * 0.34, th * 0.44),
        ], col, "frame", m=14, e=2.8)

    # ── cranks + chainring (a pedal bike, not a motorbike) ──
    if pedals and not engine:
        cr = max(0.06, wr * 0.30)
        bm = bmesh.new()
        v, e = ngon_ring(bm, (ox + ww * 0.5 + tube_r, bb[1], bb[2]), cr, 20, axis="X")
        bm.faces.new(v[::-1])
        v, e = extrude_ring(bm, e, translate=(tube_r * 0.5, 0, 0))
        bm.faces.new(v)
        bm.faces.ensure_lookup_table()
        for cap in (min(bm.faces, key=lambda f: f.calc_center_median().x),
                    max(bm.faces, key=lambda f: f.calc_center_median().x)):
            try:
                notch(bm, cap, inset=cr * 0.30, depth=tube_r * 0.12)   # spider recess
            except Exception:
                pass
        tag(_kobj(bm, "chainring", chrome, "frame", bevel=0.002), "frame")
        for sx in (1, -1):
            cx = ox + sx * (ww * 0.5 + tube_r * 2.2)
            arm_z = bb[2] + sx * cr * 0.9
            bm = bmesh.new()
            v, e = quad_ring(bm, (cx, bb[1], bb[2]), tube_r * 1.4, tube_r * 2.2, axis="Z")
            bm.faces.new(v)
            v, e = extrude_ring(bm, e, translate=(0, 0, sx * cr * 0.5), scale=(0.9, 0.75, 1))
            v, e = extrude_ring(bm, e, translate=(0, 0, sx * cr * 0.4), scale=(1.0, 0.9, 1))
            bm.faces.new(v[::-1])
            tag(_kobj(bm, "crank_%d" % (sx > 0), chrome, "frame"), "frame")
            bm = bmesh.new()
            v, e = quad_ring(bm, (cx + sx * tube_r * 1.6, bb[1], arm_z), 0.09, 0.05, axis="Z")
            bm.faces.new(v)
            v, e = extrude_ring(bm, e, translate=(0, 0, -0.020))
            bm.faces.new(v[::-1])
            tag(_kobj(bm, "pedal_%d" % (sx > 0), dark, "frame"), "frame")

    # bone table for the rigger — positions from the geometry above, NOT from
    # obj.location (every bmesh part here sits at the origin with world-coord verts)
    BIKE_BONES = [
        ("root", (ox, 0, 0.02), (ox, 0, 0.30), None),
        ("frame", (ox, bb[1], wr * 0.95), (ox, bb[1] - wb * 0.30, wr * 0.95), "root"),
        ("steer", (ox, head_b[1], head_b[2] - fh * 0.30),
         (ox, head_b[1], head_t[2] + fh * 0.25), "root"),
        ("Wheel.F", (ox, front_y, wr), (ox + 0.30, front_y, wr), "steer"),
        ("Wheel.R", (ox, rear_y, wr), (ox + 0.30, rear_y, wr), "root"),
    ]
    return parts
'''

_FURNITURE_SRC = '''
def _fobj(bm, name, rgb, role, bevel=0.004, subsurf=0):
    """Weld a furniture-part bmesh into a named, coloured, finished object — every
    panel, slab, leg and knob goes through here, none through
    box()/cyl()/cone()/sphere()."""
    import bmesh as _bmesh
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    _into(o)
    colour(o, rgb)
    finish(o, smooth=True, subsurf=subsurf, bevel=bevel)
    o["role"] = role
    return o

def build(p, ox):
    """One parametric furniture piece, every part edge-loop sculpted: slabs carry a
    moulded edge (the top face is inset and lifted, so the rim reads as a profile
    instead of a cut cube), legs are TURNED on a stepped profile of revolution
    (square post block -> cove -> vase swell -> taper -> foot), a legged piece gets
    the apron rail that stops a top from floating on sticks, carcasses get a recessed
    panel and a plinth, backs get a shaped crest rail, and drawer fronts get a
    recessed panel with a turned knob. The same knobs span a table, desk,
    chair/stool/bench/sofa (seat[+back]), open bookcase (legs=0, shelves>0) and a
    dresser/desk drawer stack (drawers>0). The primary horizontal surface
    (top / seat / lid) sits with its top face at z=height; legs or a carcass carry it
    to the floor. Static decor, so it gets the no-op rigger; drawer fronts are still
    named Drawer.* by role for later articulation."""
    import bmesh, math
    w, d, h = p["width"], p["depth"], p["height"]
    top_t = max(0.02, p["top_t"]); leg_r = p["leg_r"]
    nlegs = max(0, min(4, int(round(p["legs"]))))
    seat = p["seat"] >= 0.5; back = p["back"] >= 0.5
    nshelves = max(0, min(6, int(round(p["shelves"]))))
    ndrawers = max(0, min(6, int(round(p["drawers"]))))
    cushion = p["cushion"] >= 0.5
    col = tuple(p["color"]); dark = (0.10, 0.10, 0.10)
    brass = (0.62, 0.50, 0.24)
    pad = tuple(min(1.0, c + 0.06) for c in col)        # lighter upholstery tint
    pad2 = tuple(min(1.0, c + 0.13) for c in col)       # loose cushions, lighter again
    parts = []

    def tag(o, role):
        o["role"] = role
        parts.append(o)
        return o

    def slab(name, cx, cy, z_top, sw, sd, t, rgb, role, moulded=True, puffy=False):
        """A horizontal panel grown down from its top face. `moulded` steps the rim
        in and out so the edge carries a profile (the difference between a table top
        and a cut cube); `puffy` bulges it instead, for a cushion, and adds the seam
        line where the upholstery is stitched."""
        if moulded and t < min(sw, sd) * 0.06:
            moulded = False        # a 15 mm shelf takes a crisp arris, not an ogee:
                                   # moulding one and shading it smooth reads as a pillow
        # A puffy pad's two bulge rings scale CUMULATIVELY (1.05 * 1.07 = 1.1235),
        # so the finished cushion used to come out 12% larger than the caller asked
        # for. Start the top ring in by exactly that factor: `sw`/`sd` then bound
        # the FINISHED pad. (The unaccounted growth is what buried each sofa
        # cushion 60 mm inside the arm beside it.)
        k = 0.8901 if puffy else (0.94 if moulded else 1.0)
        bm = bmesh.new()
        v, e = quad_ring(bm, (cx, cy, z_top), sw * k, sd * k, axis="Z")
        bm.faces.new(v[::-1])
        if puffy:
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.16), scale=(1.05, 1.05, 1))
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.30), scale=(1.07, 1.07, 1))
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.34))
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.20), scale=(0.90, 0.90, 1))
        elif moulded:
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.22), scale=(1.032, 1.032, 1))
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.30), scale=(1.032, 1.032, 1))
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.28), scale=(0.985, 0.985, 1))
            v, e = extrude_ring(bm, e, translate=(0, 0, -t * 0.20), scale=(0.97, 0.97, 1))
        else:
            v, e = extrude_ring(bm, e, translate=(0, 0, -t))
        bm.faces.new(v)
        if puffy:
            bm.faces.ensure_lookup_table()
            top = max(bm.faces, key=lambda f: f.calc_center_median().z)
            try:
                notch(bm, top, inset=min(sw, sd) * 0.10, depth=t * 0.06)   # stitch seam
            except Exception:
                pass
        return tag(_fobj(bm, name, rgb, role, bevel=min(0.006, t * 0.10)), role)

    def turned_leg(name, cx, cy, z0, z1, r, rgb, square_top=True):
        """A leg turned on a profile of revolution: a square post block at the top
        (where the apron meets it), then cove, vase swell, long taper and a foot pad.
        A plain cylinder is the single biggest tell that furniture was assembled from
        primitives."""
        span = max(0.05, z1 - z0)
        prof = [(0.00, r * 0.95), (0.04, r * 1.06), (0.07, r * 1.06), (0.10, r * 0.78),
                (0.16, r * 0.62), (0.24, r * 0.92), (0.33, r * 1.00), (0.44, r * 0.80),
                (0.72, r * 0.58), (0.90, r * 0.66), (0.97, r * 0.74), (1.00, r * 0.62)]
        o = loft(name, [((cx, cy, z1 - span * t), rr) for t, rr in prof], rgb, n=12)
        finish(o, smooth=True, bevel=0.002)
        if square_top:                       # the block the apron tenons into
            bm = bmesh.new()
            v, e = quad_ring(bm, (cx, cy, z1), r * 2.0, r * 2.0, axis="Z")
            bm.faces.new(v[::-1])
            v, e = extrude_ring(bm, e, translate=(0, 0, -span * 0.12))
            bm.faces.new(v)
            tag(_fobj(bm, name + "_block", rgb, "leg", bevel=min(0.004, r * 0.10)), "leg")
        return tag(o, "leg")

    def panel(name, cx, cy, cz, pw, pd, ph, rgb, role, recess=True, axis_face="X"):
        """An upright carcass panel with a recessed field on its outward face — the
        frame-and-panel look every real case piece has, instead of a flat slab."""
        bm = bmesh.new()
        v, e = quad_ring(bm, (cx, cy, cz - ph * 0.5), pw, pd, axis="Z")
        bm.faces.new(v)
        v, e = extrude_ring(bm, e, translate=(0, 0, ph))
        bm.faces.new(v[::-1])
        if recess:
            bm.faces.ensure_lookup_table()
            key = ((lambda f: f.calc_center_median().x) if axis_face == "X"
                   else (lambda f: f.calc_center_median().y))
            for f in (max(bm.faces, key=key), min(bm.faces, key=key)):
                try:
                    notch(bm, f, inset=min(pw, pd, ph) * 0.30, depth=min(0.012, pd * 0.25))
                except Exception:
                    pass
        return tag(_fobj(bm, name, rgb, role, bevel=0.004), role)

    surf_top = h
    pan_t = top_t * (2.4 if (seat and cushion) else 1.0)
    leg_top = surf_top - pan_t
    side_t = max(0.03, top_t)
    puffy = bool(seat and cushion)
    br_h = max(0.30, h * 0.95)          # backrest height above the seat
    # A padded seat WITH a back is a sofa/armchair, and a sofa is defined by its
    # ARMS: a rolled bolster carried on an upholstered outer panel, flanking the
    # seat. The cushions therefore span the width BETWEEN the arms — running them
    # out to the outer edge leaves each arm buried inside the end cushion, which
    # is what made this piece read as a padded bench.
    arms = bool(cushion and back)
    arm_w = min(max(0.10, d * 0.20), w * 0.24) if arms else 0.0
    roll_r = arm_w * 0.46               # the roll that caps an arm / the back
    arm_h = max(0.15, br_h * 0.52)      # arm top above the seat
    back_t = max(0.06, side_t * 2.0) if arms else 0.0     # upholstered back frame
    bc_t = max(0.10, d * 0.16) if arms else 0.0           # back-cushion thickness
    inner_w = w - arm_w * 2.0
    # how many seat cushions: one for an armchair, two or three across a sofa
    n_cush = max(1, min(3, int(round(inner_w / max(0.55, d * 0.90))))) if puffy else 1
    if puffy:
        plat_t = pan_t * 0.34
        cush_t = pan_t - plat_t
        slab("top", ox, 0.0, surf_top - cush_t, w, d, plat_t, pad, "surface",
             moulded=False)
        # a seat cushion runs from the front edge back to the FACE of the back
        # cushion, not the full depth — at full depth it buries itself in the back
        y_front = d * 0.5 - 0.006
        y_rear = -d * 0.5 + (back_t + bc_t if arms else 0.02)
        cd = max(d * 0.30, y_front - y_rear)
        cy = y_front - cd * 0.5
        gap = inner_w * 0.012
        cw = (inner_w - gap * (n_cush - 1)) / n_cush
        for i in range(n_cush):
            slab("cushion_%d" % i, ox - inner_w * 0.5 + cw * (i + 0.5) + gap * i, cy,
                 surf_top, cw, cd, cush_t, pad2, "surface",
                 moulded=False, puffy=True)
    else:
        slab("top", ox, 0.0, surf_top, w, d, pan_t, col, "surface")

    if nlegs == 0 and nshelves > 0:
        # open bookcase: panelled sides + back + plinth + shelves with a front edge
        for sx in (1, -1):
            panel("side_%d" % (sx > 0), ox + sx * (w * 0.5 - side_t * 0.5), 0.0,
                  leg_top * 0.5 + side_t, side_t, d, max(0.05, leg_top - side_t),
                  col, "body", axis_face="X")
        panel("back_panel", ox, d * 0.5 - side_t * 0.5, leg_top * 0.5 + side_t,
              w - side_t * 2.0, side_t, max(0.05, leg_top - side_t), col, "body",
              axis_face="Y")
        slab("plinth", ox, 0.0, side_t, w * 0.98, d * 0.96, side_t, col, "body")
        for i in range(nshelves):
            z = side_t + (leg_top - side_t) * (i + 1) / (nshelves + 1)
            slab("shelf_%d" % i, ox, 0.0, z, w - side_t * 2.2, d * 0.92,
                 max(0.015, top_t * 0.8), col, "shelf")
    elif nlegs == 0:
        panel("body", ox, 0.0, max(0.05, leg_top) * 0.5 + side_t * 0.5,
              w, d, max(0.05, leg_top - side_t * 0.5), col, "body", axis_face="Y")
        slab("plinth", ox, 0.0, side_t * 0.9, w * 0.96, d * 0.94, side_t * 0.9, col, "body")
    elif nlegs == 1:
        # pedestal: a turned column on a moulded foot disc
        cr = max(0.04, w * 0.12)
        bm = bmesh.new()
        v, e = ngon_ring(bm, (ox, 0, 0), w * 0.34, 16, axis="Z")
        bm.faces.new(v)
        v, e = extrude_ring(bm, e, translate=(0, 0, max(0.02, leg_top * 0.05)))
        v, e = extrude_ring(bm, e, translate=(0, 0, max(0.015, leg_top * 0.035)),
                            scale=0.72)
        bm.faces.new(v[::-1])
        tag(_fobj(bm, "foot", col, "leg", bevel=0.004), "leg")
        base_h = max(0.035, leg_top * 0.085)
        prof = [(0.00, cr * 0.62), (0.10, cr * 0.80), (0.22, cr * 1.00), (0.42, cr * 0.74),
                (0.72, cr * 0.60), (0.88, cr * 0.78), (1.00, cr * 0.86)]
        o = loft("column", [((ox, 0, base_h + (leg_top - base_h) * t), rr)
                            for t, rr in prof], col, n=16)
        finish(o, smooth=True, bevel=0.003)
        tag(o, "leg")
    else:
        lr = max(0.02, leg_r)
        lx = w * 0.5 - lr * 1.8; ly = d * 0.5 - lr * 1.8
        coords = ([(0, ly), (lx, -ly), (-lx, -ly)] if nlegs == 3
                  else [(lx, ly), (-lx, ly), (lx, -ly), (-lx, -ly)])
        for i, (cx, cy) in enumerate(coords):
            turned_leg("leg_%d" % i, ox + cx, cy, 0.0, leg_top, lr, col)
        # apron rails between the legs, tucked under the top
        ap_h = max(0.03, min(leg_top * 0.22, top_t * 1.8))
        ap_z = leg_top - lr * 0.4
        ap_t = max(0.012, lr * 0.55)
        if nlegs == 4:
            for sy in (1, -1):
                slab("apron_y_%d" % (sy > 0), ox, sy * ly, ap_z, abs(lx) * 2.0, ap_t,
                     ap_h, col, "body", moulded=False)
            for sx in (1, -1):
                slab("apron_x_%d" % (sx > 0), ox + sx * lx, 0.0, ap_z, ap_t,
                     abs(ly) * 2.0, ap_h, col, "body", moulded=False)

    # backrest. Upholstered (cushion) means a rolled back and a pair of rolled
    # arms — stiles with open slats between them is a garden bench, not a sofa.
    # Bare timber keeps the joined chair back: stiles, crest rail, mid rail.
    if back:
        by = -d * 0.5 + side_t
        st_w = max(0.03, w * 0.06)
        if arms:
            def bolster(name, a, b, r):
                """The roll that caps an arm or a back — a lofted tube along the
                part's own axis with both ends drawn in, so it terminates in a
                scroll instead of a cut face. This is the part that makes an
                upholstered panel read as an ARM rather than a stray cushion."""
                import mathutils
                a = mathutils.Vector(a); b = mathutils.Vector(b)
                if (b - a).length < 1e-6:
                    return
                u = (b - a).normalized()
                o = loft(name, [(a - u * r * 0.10, r * 0.55),
                                (a + u * r * 0.55, r * 0.98),
                                (b - u * r * 0.55, r * 0.98),
                                (b + u * r * 0.10, r * 0.55)], pad, n=16)
                finish(o, smooth=True, bevel=0.002)
                tag(o, "back")

            arm_top = surf_top + arm_h
            arm_d = d * 0.96
            arm_z0 = surf_top - pan_t            # the arm lands on the frame rail
            for sx in (1, -1):
                ax = ox + sx * (w * 0.5 - arm_w * 0.5)
                slab("arm_%d" % (sx > 0), ax, 0.0, arm_top - roll_r, arm_w, arm_d,
                     max(0.05, arm_top - roll_r - arm_z0), pad, "back",
                     moulded=False, puffy=True)
                bolster("arm_roll_%d" % (sx > 0),
                        (ax, arm_d * 0.5, arm_top - roll_r),
                        (ax, -arm_d * 0.5, arm_top - roll_r), roll_r)
            # the back is upholstered too: frame panel + rolled top rail. NOT
            # puffy — a 7% bulge on a 2 m panel would swell it past the arms.
            back_top = surf_top + br_h
            byc = -d * 0.5 + back_t * 0.5
            slab("back", ox, byc, back_top - roll_r, w, back_t,
                 max(0.05, back_top - roll_r - arm_z0), pad, "back", moulded=False)
            bolster("back_roll", (ox - w * 0.5 + roll_r * 0.3, byc, back_top - roll_r),
                    (ox + w * 0.5 - roll_r * 0.3, byc, back_top - roll_r), roll_r)
            # back cushions match the seat cushions across the width, inside the arms
            gap = inner_w * 0.012
            cw = (inner_w - gap * (n_cush - 1)) / max(1, n_cush)
            for i in range(n_cush):
                slab("back_cushion_%d" % i,
                     ox - inner_w * 0.5 + cw * (i + 0.5) + gap * i,
                     -d * 0.5 + back_t + bc_t * 0.5,
                     surf_top + br_h * 0.90, cw, bc_t, br_h * 0.90, pad2,
                     "back", moulded=False, puffy=True)
        else:
            for sx in (1, -1):
                slab("back_stile_%d" % (sx > 0), ox + sx * (w * 0.5 - st_w * 0.5), by,
                     surf_top + br_h, st_w, side_t * 1.2, br_h, col, "back",
                     moulded=False)
            slab("back", ox, by, surf_top + br_h, w - st_w * 1.6, side_t * 1.15,
                 br_h * 0.34, col, "back")
            slab("back_rail", ox, by, surf_top + br_h * 0.42, w - st_w * 1.6,
                 side_t * 1.05, br_h * 0.22, col, "back")

    # drawer fronts (desk pedestal / dresser) on the front (-Y) face
    if ndrawers > 0:
        if nlegs == 0:
            col_w = w; col_x = ox
        else:                                   # legged desk -> a drawer pedestal on +X
            col_w = w * 0.42; col_x = ox + (w * 0.5 - col_w * 0.5) - side_t
            panel("pedestal", col_x, 0.0, leg_top * 0.5, col_w, d * 0.96, leg_top,
                  col, "body", axis_face="X")
        front_y = -d * 0.5 - 0.004
        dh = leg_top / ndrawers
        for i in range(ndrawers):
            z = dh * (i + 0.5)
            bm = bmesh.new()
            v, e = quad_ring(bm, (col_x, front_y, z), col_w * 0.88, dh * 0.84, axis="Y")
            bm.faces.new(v)
            v, e = extrude_ring(bm, e, translate=(0, -0.022, 0))
            bm.faces.new(v[::-1])
            bm.faces.ensure_lookup_table()
            face = min(bm.faces, key=lambda f: f.calc_center_median().y)
            try:
                notch(bm, face, inset=min(col_w * 0.88, dh * 0.84) * 0.16,
                      depth=0.006)                       # recessed drawer panel
            except Exception:
                pass
            tag(_fobj(bm, "Drawer.%d" % i, col, "drawer", bevel=0.004), "drawer")
            kr = max(0.014, min(0.030, dh * 0.16))
            kn = loft("knob_%d" % i,
                      [((col_x, front_y - 0.022, z), kr * 0.45),
                       ((col_x, front_y - 0.022 - kr * 0.9, z), kr * 0.40),
                       ((col_x, front_y - 0.022 - kr * 1.5, z), kr * 1.00),
                       ((col_x, front_y - 0.022 - kr * 2.3, z), kr * 0.80)],
                      brass, n=12)
            finish(kn, smooth=True, bevel=0.001)
            tag(kn, "drawer")
    return parts
'''

_WEAPON_SRC = '''
def _wobj(bm, name, rgb, role, bevel=0.004, subsurf=0):
    """Weld a weapon-part bmesh into a named, coloured, finished object — every
    weapon part goes through here, none through box()/cyl()/cone()/sphere()."""
    import bmesh as _bmesh
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    _into(o)
    colour(o, rgb)
    finish(o, smooth=True, subsurf=subsurf, bevel=bevel)
    o["role"] = role
    return o

def build(p, ox):
    """One parametric firearm: receiver+barrel welded into ONE sculpted spine plus
    bolt-on attachment slots (scope / stock / muzzle device / magazine / grips),
    every part built by edge-loop extrusion (quad_ring/ngon_ring + extrude_ring),
    never a bare primitive. The same knobs span a pistol (no stock, short barrel),
    an SMG, a rifle/carbine and a sniper (long barrel + scope). Forward is +Y (the
    muzzle direction), Z up. Every part is named by role (body / muzzle / stock /
    scope / magazine / grip / foregrip) so a fire-point or runtime attachment swap
    can key off the names later. Static prop, so it gets the no-op rigger."""
    import bmesh
    L = p["length"]; cal = max(0.012, p["caliber"]); blen = p["barrel_len"]
    nstock = max(0, min(2, int(round(p["stock"]))))
    nmuzzle = max(0, min(2, int(round(p["muzzle"]))))
    nmag = max(0, min(2, int(round(p["magazine"]))))
    scope = p["scope"] >= 0.5; grip = p["grip"] >= 0.5; foregrip = p["foregrip"] >= 0.5
    col = tuple(p["color"]); black = (0.05, 0.05, 0.05); metal = (0.32, 0.33, 0.35)
    parts = []

    bz = 0.0                                    # bore line height
    recv_len = max(0.10, L * 0.55)              # receiver length (along Y)
    recv_w = max(0.03, cal * 1.8)
    recv_h = max(0.05, cal * 3.2)
    front_y = recv_len * 0.5
    rear_y = -recv_len * 0.5

    # ── receiver + barrel: ONE welded spine, grown by edge-loop extrusion ──
    bm = bmesh.new()
    v0, e0 = quad_ring(bm, (ox, rear_y, bz), recv_w, recv_h)
    bm.faces.new(v0[::-1])                                       # rear cap (-Y facing)
    v1, e1 = extrude_ring(bm, e0, translate=(0, recv_len, 0))    # receiver block, no taper
    bore_w = min(cal * 2.2, recv_w * 0.9); bore_h = min(cal * 2.2, recv_h * 0.85)
    shoulder_dy = min(0.02, blen * 0.15)
    v2, e2 = extrude_ring(bm, e1, translate=(0, shoulder_dy, 0),  # shoulder -> bore diameter
                          scale=(bore_w / recv_w, bore_h / recv_h, 1))
    run_dy = blen - shoulder_dy
    v3, e3 = extrude_ring(bm, e2, translate=(0, run_dy, 0))       # barrel run, constant bore
    muzzle_y = front_y + blen
    if nmuzzle == 0:                                              # bare muzzle: crown + cap
        crown_dy = min(0.01, blen * 0.05)
        v4, e4 = extrude_ring(bm, e3, translate=(0, crown_dy, 0), scale=(0.85, 0.85, 1))
        bm.faces.new(v4)
    else:                                                         # muzzle device caps it instead
        bm.faces.new(v3)
    bevel_edges(bm, e1 + e2, min(0.006, bore_w * 0.15), 2)        # crisp shoulder/receiver step
    body = _wobj(bm, "receiver", col, "body")
    parts.append(body)

    # ── muzzle device: stepped/tapered n-gon tube, not a bare cyl() ──
    if nmuzzle == 1:                                              # muzzle brake / compensator
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, (ox, muzzle_y, bz), cal * 1.15, 12)
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=(0, 0.015, 0), scale=1.4)   # flange out
        v2, e2 = extrude_ring(bm, e1, translate=(0, 0.03, 0))               # brake body
        v3, e3 = extrude_ring(bm, e2, translate=(0, 0.01, 0), scale=0.75)   # crown taper
        bm.faces.new(v3)
        mz = _wobj(bm, "muzzle", black, "muzzle")
        parts.append(mz)
    elif nmuzzle == 2:                                            # suppressor
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, (ox, muzzle_y, bz), cal * 1.3, 14)
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=(0, 0.02, 0), scale=1.7)    # rear cap flange
        v2, e2 = extrude_ring(bm, e1, translate=(0, 0.16, 0))               # main tube run
        v3, e3 = extrude_ring(bm, e2, translate=(0, 0.03, 0), scale=0.8)    # front taper
        bm.faces.new(v3)
        mz = _wobj(bm, "muzzle", black, "muzzle")
        parts.append(mz)

    # ── stock (rearward, -Y): tapered extrusion, skeleton variant flares mid-length ──
    if nstock >= 1:
        slen = max(0.10, L * 0.32)
        sz0 = bz - recv_h * 0.1
        bm = bmesh.new()
        if nstock == 1:                         # fixed solid stock: taper toward the butt
            w0, h0 = recv_w * 0.9, recv_h * 1.5
            v0, e0 = quad_ring(bm, (ox, rear_y, sz0), w0, h0)
            bm.faces.new(v0)
            v1, e1 = extrude_ring(bm, e0, translate=(0, -slen * 0.6, 0))
            v2, e2 = extrude_ring(bm, e1, translate=(0, -slen * 0.4, 0), scale=(1.15, 1.0, 1.3))
            bm.faces.new(v2[::-1])
            stk = _wobj(bm, "stock", col, "stock")
        else:                                   # skeleton/folding: thin, flares at the shoulder hook
            w0, h0 = recv_w * 0.55, recv_h * 0.5
            v0, e0 = quad_ring(bm, (ox, rear_y, sz0), w0, h0)
            bm.faces.new(v0)
            v1, e1 = extrude_ring(bm, e0, translate=(0, -slen * 0.55, 0))
            v2, e2 = extrude_ring(bm, e1, translate=(0, -slen * 0.30, 0), scale=(1.0, 1.7, 1.0))
            v3, e3 = extrude_ring(bm, e2, translate=(0, -slen * 0.15, 0), scale=(1.0, 0.6, 1.0))
            bm.faces.new(v3[::-1])
            stk = _wobj(bm, "stock", black, "stock")
        parts.append(stk)

    # ── pistol grip (down, behind the trigger): tapered swell + texture notch ──
    if grip:
        gy = rear_y + recv_len * 0.18; gz0 = bz - recv_h * 0.5
        bm = bmesh.new()
        v0, e0 = quad_ring(bm, (ox, gy, gz0), recv_w * 0.85, 0.05, axis="Z")
        bm.faces.new(v0)
        v1, e1 = extrude_ring(bm, e0, translate=(0, 0, -0.09), scale=(1.15, 1.15, 1))  # swell
        v2, e2 = extrude_ring(bm, e1, translate=(0, 0, -0.06), scale=(0.7, 0.7, 1))    # narrow to butt
        bm.faces.new(v2[::-1])
        bm.faces.ensure_lookup_table()
        front_face = max(bm.faces, key=lambda f: f.calc_center_median().y)
        try:
            notch(bm, front_face, inset=0.2, depth=0.006)      # stipple texture panel
        except Exception:
            pass
        g = _wobj(bm, "grip", black, "grip")
        parts.append(g)

    # ── trigger guard: a swept rod bow (round cross-section, not a flat ribbon —
    # a flat quad-ring folded through a bend reads as broken shards from most
    # angles; a round rod stays legible bent) ──
    tgy = rear_y + recv_len * 0.30; tgz = bz - recv_h * 0.5 - 0.01
    gr = max(0.003, recv_w * 0.06)
    bm = bmesh.new()
    v0, e0 = ngon_ring(bm, (ox, tgy - 0.06, tgz), gr, 8)
    bm.faces.new(v0[::-1])
    v1, e1 = extrude_ring(bm, e0, translate=(0, 0.02, -0.03))     # down from the receiver
    v2, e2 = extrude_ring(bm, e1, translate=(0, 0.05, -0.01))     # forward along the bottom
    v3, e3 = extrude_ring(bm, e2, translate=(0, 0.03, 0.03))      # sweep back up
    v4, e4 = extrude_ring(bm, e3, translate=(0, 0.0, 0.03))       # close toward the receiver
    bm.faces.new(v4)
    tg = _wobj(bm, "trigger_guard", black, "body")
    parts.append(tg)

    # ── magazine (down, ahead of the grip): tapered body, drum variant is round ──
    if nmag == 1:                               # box magazine, slight taper + seam notch
        my = recv_len * 0.05; mz0 = bz - recv_h * 0.5
        bm = bmesh.new()
        v0, e0 = quad_ring(bm, (ox, my, mz0), recv_w * 0.75, 0.04, axis="Z")
        bm.faces.new(v0)
        v1, e1 = extrude_ring(bm, e0, translate=(0, 0, -0.15))
        v2, e2 = extrude_ring(bm, e1, translate=(0, 0, -0.045), scale=(0.85, 0.9, 1))
        bm.faces.new(v2[::-1])
        bm.faces.ensure_lookup_table()
        side_face = max(bm.faces, key=lambda f: f.calc_center_median().x)
        try:
            notch(bm, side_face, inset=0.35, depth=0.003)      # seam line
        except Exception:
            pass
        mg = _wobj(bm, "magazine", black, "magazine")
        parts.append(mg)
    elif nmag == 2:                             # drum magazine: round body, axle along X
        my = recv_len * 0.05; mz0 = bz - recv_h * 0.5 - 0.10
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, (ox, my, mz0), 0.05, 16, axis="X")
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=(recv_w * 0.30, 0, 0), scale=1.5)   # drum bulge
        v2, e2 = extrude_ring(bm, e1, translate=(recv_w * 0.30, 0, 0), scale=0.67)  # back down
        bm.faces.new(v2)
        mg = _wobj(bm, "magazine", black, "magazine")
        parts.append(mg)

    # ── vertical foregrip under the handguard: same tapered-swell treatment ──
    if foregrip:
        fy = front_y + blen * 0.30; fz0 = bz - recv_h * 0.5
        bm = bmesh.new()
        v0, e0 = quad_ring(bm, (ox, fy, fz0), recv_w * 0.55, 0.04, axis="Z")
        bm.faces.new(v0)
        v1, e1 = extrude_ring(bm, e0, translate=(0, 0, -0.07), scale=(1.1, 1.1, 1))
        v2, e2 = extrude_ring(bm, e1, translate=(0, 0, -0.04), scale=(0.75, 0.75, 1))
        bm.faces.new(v2[::-1])
        fg = _wobj(bm, "foregrip", black, "foregrip")
        parts.append(fg)

    # ── optic on the top rail: n-gon scope tube with turret bumps + rail mounts ──
    if scope:
        sy = recv_len * 0.1; sz = bz + recv_h * 0.5 + 0.05; slen2 = L * 0.25
        r0 = cal * 1.4
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, (ox, sy - slen2 * 0.5, sz), r0 * 1.15, 14)  # objective bell
        bm.faces.new(v0[::-1])
        v1, e1 = extrude_ring(bm, e0, translate=(0, slen2 * 0.18, 0), scale=r0 / (r0 * 1.15))
        v2, e2 = extrude_ring(bm, e1, translate=(0, slen2 * 0.24, 0), scale=1.25)   # windage turret
        v3, e3 = extrude_ring(bm, e2, translate=(0, slen2 * 0.24, 0), scale=0.8)
        v4, e4 = extrude_ring(bm, e3, translate=(0, slen2 * 0.24, 0), scale=1.1)    # ocular bell
        bm.faces.new(v4)
        sc = _wobj(bm, "scope", metal, "scope")
        parts.append(sc)
        for i, ry in enumerate((sy - L * 0.08, sy + L * 0.08)):
            bm = bmesh.new()
            v0, e0 = quad_ring(bm, (ox, ry, bz + recv_h * 0.5), recv_w * 0.45, 0.03, axis="Z")
            bm.faces.new(v0)
            v1, e1 = extrude_ring(bm, e0, translate=(0, 0, 0.05))
            bm.faces.new(v1[::-1])
            bm.faces.ensure_lookup_table()
            top_face = max(bm.faces, key=lambda f: f.calc_center_median().z)
            try:
                notch(bm, top_face, inset=0.3, depth=0.004)   # rail-clamp groove
            except Exception:
                pass
            mt = _wobj(bm, "scope_mount_%d" % i, black, "scope")
            parts.append(mt)
    return parts
'''

_BOAT_SRC = '''
def _sobj(bm, name, rgb, bevel=0.008, subsurf=0):
    """Weld a boat-part bmesh into a named, coloured, finished object — every
    superstructure / spar / cloth / foil part goes through here, so none of them can
    fall back to a bare box()/cyl()/cone()/sphere() union."""
    import bmesh as _bmesh
    _bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name + "_m")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    _into(o)
    colour(o, rgb)
    finish(o, smooth=True, subsurf=subsurf, bevel=bevel)
    return o

def _ring_scale(axis, k):
    """extrude_ring's (sx, sy, sz) for a uniform in-plane scale of a ring that lies
    perpendicular to `axis` — the ring's own plane is the other two axes, and the
    axis component must stay 1.0 or the section thickens as it tapers."""
    if axis == "Y":
        return (k, 1.0, k)
    if axis == "X":
        return (1.0, k, k)
    return (k, k, 1.0)

def _foil_ring(bm, center, chord, thick, n=12):
    """A closed n-vert boundary ring shaped like a symmetric NACA-00xx aerofoil
    section, lying in the XY plane (chord along Y, thickness along X) so it grows
    along Z — the cross-section rudder and propeller blades start from, in place of
    a flat rectangle. Sampled on half-steps so no vertex lands exactly on the
    leading/trailing edge, where the two surfaces meet and a coincident vertex pair
    would collapse into the zero-length edge that breaks bridge_loops."""
    cx, cy, cz = center
    verts = []
    for k in range(n):
        a = 2.0 * _PI * (k + 0.5) / n
        xc = 0.5 * (1.0 - _cos(a))
        yt = 5.0 * thick * (0.2969 * xc ** 0.5 - 0.1260 * xc - 0.3516 * xc ** 2
                            + 0.2843 * xc ** 3 - 0.1015 * xc ** 4)
        side = 1.0 if a < _PI else -1.0
        verts.append(bm.verts.new((cx + side * yt, cy + (xc - 0.5) * chord, cz)))
    edges = [bm.edges.new((verts[i], verts[(i + 1) % n])) for i in range(n)]
    return verts, edges

def build(p, ox):
    """One parametric watercraft, every part edge-loop sculpted: a lofted U-section
    hull (tapered to bow/stern, keel at z=0) + deck, then bolt-on structural slots
    that span a rowboat (no mast/sail/motor), a sailboat (tapered masts + yards +
    bellied square sails), a motorboat/tug (foil-bladed propeller + deckhouse
    [+ funnel]). Bow is -Y. Superstructure grows from its own floor ring with a
    recessed window band and an overhanging eave; masts, yards and the rudder stock
    are tapered n-gon spars; sails are doubly-curved cloth volumes; the funnel is a
    hollow stack with a closed collar; rudder and propeller blades are NACA foils,
    the propeller's twisted so pitch washes out toward the tip. The rudder
    (`RudderSteer`) and any propeller (`Propeller`) are named so the
    VehicleBoneDriver yaws/spins them. Knobs: hull_len/_beam/_depth, deck_h, mast_h
    + structural masts/sails/prop/cabin/rudder/funnel."""
    import bmesh, math, mathutils
    L = p["hull_len"]; beam = p["hull_beam"]; depth = p["hull_depth"]
    deck_h = p["deck_h"]; mast_h = p["mast_h"]
    nmast = max(0, min(3, int(round(p["masts"]))))
    sails = p["sails"] >= 0.5; prop = p["prop"] >= 0.5
    cabin = p["cabin"] >= 0.5; rudder = p["rudder"] >= 0.5; funnel = p["funnel"] >= 0.5
    hullc = tuple(p["color"]); deckc = tuple(min(1.0, c * 1.4 + 0.05) for c in p["color"])
    mastc = (0.20, 0.13, 0.08); sailc = (0.93, 0.93, 0.93)
    metal = (0.30, 0.30, 0.32); dark = (0.08, 0.08, 0.08); red = (0.65, 0.12, 0.10)
    parts = []

    def tag(o, bone, role="boat"):
        o["bone"] = bone; o["role"] = role; parts.append(o); return o

    def spar(name, start, end, sections, rgb, ring_axis, bone="Hull", role="boat",
             n=8, bevel=0.004):
        """One tapered round spar grown by n-gon-ring extrusion: a heel ring, then one
        extruded section per (t, radius) stop marching along start -> end. A mast
        tapers to a flared truck, a yard swells at the waist and tapers to both tips,
        a rudder stock is a near-parallel post — all three from this one path."""
        bm = bmesh.new()

        def at(t):
            return tuple(start[i] + (end[i] - start[i]) * t for i in range(3))

        t0, r0 = sections[0]
        v, e = ngon_ring(bm, at(t0), r0, n, axis=ring_axis)
        bm.faces.new(v[::-1])
        pt, pr = t0, r0
        for t, r in sections[1:]:
            a0 = at(pt); a1 = at(t)
            v, e = extrude_ring(bm, e,
                                translate=(a1[0] - a0[0], a1[1] - a0[1], a1[2] - a0[2]),
                                scale=_ring_scale(ring_axis, r / pr))
            pt, pr = t, r
        bm.faces.new(v)
        return tag(_sobj(bm, name, rgb, bevel=bevel), bone, role)

    def house(name, cy, w, ln, h, z0, rgb, rake=0.0, door=False):
        """A deckhouse / wheelhouse as ONE welded shell grown from its floor ring:
        kick panel, a recessed window band (a real inset groove running right round
        the house, not a painted stripe), header, tumblehome, an overhanging eave and
        a crowned roof. `rake` leans each section aft as it rises, giving a
        wheelhouse its slanted screen; `door` recesses a doorway into the aft face."""
        bm = bmesh.new()
        v, e = quad_ring(bm, (ox, cy, z0), w, ln, axis="Z")
        bm.faces.new(v)
        v, e = extrude_ring(bm, e, translate=(0, rake * h * 0.28, h * 0.28))
        v, e = extrude_ring(bm, e, translate=(0, 0, h * 0.04), scale=(0.93, 0.95, 1))
        v, e = extrude_ring(bm, e, translate=(0, rake * h * 0.34, h * 0.34))
        v, e = extrude_ring(bm, e, translate=(0, 0, h * 0.04), scale=(1 / 0.93, 1 / 0.95, 1))
        v, e = extrude_ring(bm, e, translate=(0, rake * h * 0.22, h * 0.22),
                            scale=(0.95, 0.96, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, 0), scale=(1.12, 1.09, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, h * 0.08), scale=(0.97, 0.98, 1))
        bm.faces.new(v[::-1])
        if door:
            bm.faces.ensure_lookup_table()
            aft = [f for f in bm.faces if f.normal.y > 0.85]
            if aft:
                low = min(aft, key=lambda ff: ff.calc_center_median().z)
                try:
                    notch(bm, low, inset=min(w, h * 0.28) * 0.28,
                          depth=min(0.05, ln * 0.10))
                except Exception:
                    pass
        return tag(_sobj(bm, name, rgb, bevel=min(0.02, h * 0.05)), "Hull")

    def sail(name, cx, cy, foot_z, sw, sh, belly, rgb):
        """A bellied square sail: horizontal cloth sections (a closed loop that arcs
        across the span on the windward face and returns one cloth-thickness behind
        it) stacked foot to head, bowing fullest just above mid-hoist and narrowing
        toward the head — a doubly-curved cloth volume, not a flat slab. Lofted
        vertex-by-vertex like the hull, since the bow depth changes per section and
        extrude_ring can only translate/scale a copy of the ring below. The grid is
        deliberately fine: a doubly-curved surface has no planar quads, and a coarse
        one renders the twist as hard-edged bright patches across the cloth (the
        bilinear-patch artifact). Out-of-plane error falls off with the square of the
        grid step, so 8x8 sections put it below the shading threshold."""
        bm = bmesh.new()
        m = 9; K = 8; th = max(0.010, sw * 0.012)
        rings = []
        for k in range(K + 1):
            t = k / K
            hw = sw * 0.5 * (1.0 - 0.14 * t)
            bow = belly * _sin(_PI * (0.10 + 0.85 * t))
            z = foot_z + sh * t
            ring = []
            for i in range(m):                              # windward face, bowed
                u = -1.0 + 2.0 * i / (m - 1)
                ring.append(bm.verts.new((cx + u * hw, cy + bow * (1.0 - u * u), z)))
            for i in range(m - 1, -1, -1):                  # leeward face, one cloth back
                u = -1.0 + 2.0 * i / (m - 1)
                ring.append(bm.verts.new((cx + u * hw, cy + bow * (1.0 - u * u) + th, z)))
            rings.append(ring)
        nv = 2 * m
        for k in range(K):
            for j in range(nv):
                try:
                    bm.faces.new([rings[k][j], rings[k][(j + 1) % nv],
                                  rings[k + 1][(j + 1) % nv], rings[k + 1][j]])
                except ValueError:
                    pass
        for cap in (rings[0][::-1], rings[-1]):
            try:
                bm.faces.new(cap)
            except ValueError:
                pass
        return tag(_sobj(bm, name, rgb, bevel=0.0), "Hull")

    # ---- hull: loft a U cross-section across stations, tapered toward the ends ----
    # Each station is a CLOSED loop — the outer skin arc, then the inner skin back
    # along the same arc one wall in — so the hull is a shell with real thickness
    # that closes at the gunwale, not a single-sided surface. The old open arc left
    # every rim edge a boundary edge: watertight nowhere, backfaces visible from
    # inboard (Unity culls them, so you saw straight through the bulwark), and the
    # headless manifold gate flagged it. The inner skin is pulled in to 0.87 of the
    # half-beam, which is where the deck's 0.86 inset lands, so the sole meets the
    # bulwark instead of floating inside it.
    hb = beam * 0.5

    def station(ty):
        """(half-beam, section depth, rim height) at fore-aft fraction ty in
        [-0.5, 0.5] — the hull's shape in one place, so the deck, the rudder and the
        propeller can all ask where the hull actually IS instead of re-deriving it.
        The plan taper is eased (cosine to the 0.62) and floored at a tenth of the
        half-beam: a straight cosine collapsed the last station to 4% and left both
        ends as a pair of thin blades with the open cockpit slot running out between
        them — a forked bow, not a stem. The rim carries real sheer (it rises toward
        bow and stern) and the section depth shortens with it, which is also the
        rocker that lifts the keel clear of the water at the ends."""
        c = max(0.0, math.cos(ty * math.pi))
        t = c ** 0.62 * 0.88 + 0.12                    # ~1 amidships, 0.12 at the tips
        return (hb * max(0.10, t), depth * max(0.20, t),
                depth + depth * 0.12 * (2.0 * abs(ty)) ** 2.2)

    bm = bmesh.new(); M = 15; N = 13; rings = []
    for i in range(M):
        ty = i / (M - 1) - 0.5
        cy = ty * L
        hbi, di, rim = station(ty)
        wall = min(di * 0.35, hb * 0.13)               # never thicker than the section
        ring = []
        for j in range(N):
            a = math.pi * j / (N - 1)                  # left rim -> round bottom -> right rim
            x = -math.cos(a) * hbi
            z = rim - math.sin(a) * di                 # rim at the sheer line, keel below it
            ring.append(bm.verts.new((ox + x, cy, z)))
        for j in range(N - 1, -1, -1):                 # inner skin, back along the same arc
            a = math.pi * j / (N - 1)
            x = -math.cos(a) * hbi * 0.87
            z = rim - math.sin(a) * (di - wall)
            ring.append(bm.verts.new((ox + x, cy, z)))
        rings.append(ring)
    NV = 2 * N
    for i in range(M - 1):
        for j in range(NV):
            try:
                bm.faces.new([rings[i][j], rings[i][(j + 1) % NV],
                              rings[i + 1][(j + 1) % NV], rings[i + 1][j]])
            except ValueError:
                pass
    for cap in (rings[0][::-1], rings[-1]):            # bow + stern caps (C-sections)
        try:
            bm.faces.new(cap)
        except ValueError:
            pass
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("Hull_m"); bm.to_mesh(me); bm.free()
    hull = bpy.data.objects.new("Hull", me); _into(hull); colour(hull, hullc); finish(hull)
    tag(hull, "Hull")

    # deck — a tapered slab that follows the hull's plan outline (inset inside the
    # rim), dropped just below it so the hull sides read as a bulwark and the deck
    # never overhangs the bow/stern. Past 60% of the half-length it sweeps up to the
    # sheer, which is what makes the ends read as a fore and after deck: left flat,
    # the open cockpit slot runs all the way out to the stem and the bow renders as
    # two thin prongs with a gap down the middle.
    deck_z = depth - max(0.10, depth * 0.18)

    def half_w(ty):
        return station(ty)[0] * 0.88               # just into the inner skin (0.87)

    def deck_top(ty):
        u = min(1.0, max(0.0, (abs(ty) - 0.30) / 0.20))
        return deck_z + (station(ty)[2] - deck_z) * (u * u * (3.0 - 2.0 * u))

    dbm = bmesh.new(); Lt = []; Rt = []; Lb = []; Rb = []
    for i in range(M):
        ty = i / (M - 1) - 0.5; cy = ty * L; hw = half_w(ty)
        zt = deck_top(ty); zb = zt - deck_h
        Lt.append(dbm.verts.new((ox - hw, cy, zt))); Rt.append(dbm.verts.new((ox + hw, cy, zt)))
        Lb.append(dbm.verts.new((ox - hw, cy, zb))); Rb.append(dbm.verts.new((ox + hw, cy, zb)))
    for i in range(M - 1):
        for quad in ([Lt[i], Rt[i], Rt[i + 1], Lt[i + 1]],           # top
                     [Lb[i + 1], Rb[i + 1], Rb[i], Lb[i]],           # bottom
                     [Lt[i], Lt[i + 1], Lb[i + 1], Lb[i]],           # left wall
                     [Rt[i + 1], Rt[i], Rb[i], Rb[i + 1]]):          # right wall
            try:
                dbm.faces.new(quad)
            except ValueError:
                pass
    for cap in ([Lt[0], Lb[0], Rb[0], Rt[0]], [Rt[-1], Rb[-1], Lb[-1], Lt[-1]]):
        try:
            dbm.faces.new(cap)
        except ValueError:
            pass
    bmesh.ops.recalc_face_normals(dbm, faces=dbm.faces[:])
    dme = bpy.data.meshes.new("Deck_m"); dbm.to_mesh(dme); dbm.free()
    deck = bpy.data.objects.new("Deck", dme); _into(deck); colour(deck, deckc)
    tag(deck, "Hull")

    # superstructure: a modest deckhouse + a smaller wheelhouse stacked on top,
    # set aft of midships — NOT one big block that merges with the hull. dh_h/dh_y
    # are computed unconditionally because the funnel stands on the deckhouse roof.
    dh_h = max(0.35, depth * 0.50)
    dh_y = L * 0.10
    if cabin:
        house("Deckhouse", dh_y, beam * 0.55, L * 0.26, dh_h, deck_z, deckc, door=True)
        wh_h = max(0.28, depth * 0.38)
        house("Wheelhouse", dh_y - L * 0.05, beam * 0.40, L * 0.13, wh_h,
              deck_z + dh_h, deckc, rake=0.10)

    # masts (along the centreline) + optional square sails, each on its own yard
    if nmast > 0 and mast_h > 0.1:
        mr = max(0.035, beam * 0.045)
        for k in range(nmast):
            my = (L * 0.3) - (L * 0.6) * (k / max(1, nmast - 1) if nmast > 1 else 0.5)
            spar("Mast_%d" % k, (ox, my, deck_z), (ox, my, deck_z + mast_h),
                 [(0.0, mr), (0.06, mr * 0.94), (0.45, mr * 0.78), (0.80, mr * 0.58),
                  (0.95, mr * 0.48), (0.965, mr * 0.95), (0.98, mr * 0.95),
                  (1.0, mr * 0.42)], mastc, "Z", n=10)
            if sails:
                for s_i in range(2):
                    sw = beam * (0.85 - 0.18 * s_i)
                    sh = mast_h * 0.32
                    foot = deck_z + mast_h * (0.30 + 0.38 * s_i) - mast_h * 0.16
                    sail("Sail_%d_%d" % (k, s_i), ox, my, foot, sw, sh,
                         -beam * 0.16, sailc)
                    yw = sw * 0.5 + beam * 0.07
                    yr = max(0.028, beam * 0.030)
                    spar("Yard_%d_%d" % (k, s_i), (ox - yw, my, foot + sh),
                         (ox + yw, my, foot + sh),
                         [(0.0, yr * 0.42), (0.12, yr * 0.72), (0.5, yr),
                          (0.88, yr * 0.72), (1.0, yr * 0.42)], mastc, "X")

    # funnel / smokestack (steamer / tug) — a hollow stack (flared base skirt, raked
    # barrel, flared cap, rim rolled over into a real bore recess) + a closed collar
    # ring for the band, aft of the wheelhouse
    if funnel:
        fy = dh_y + L * 0.06                       # ON the deckhouse roof, not off its aft edge
        fbase = deck_z + (dh_h if cabin else 0.0)
        fh = depth * 1.30
        fr = max(0.06, beam * 0.11)
        bm = bmesh.new()
        v, e = ngon_ring(bm, (ox, fy, fbase), fr * 1.20, 14, axis="Z")
        bm.faces.new(v)
        v, e = extrude_ring(bm, e, translate=(0, 0, fh * 0.06), scale=(1 / 1.2, 1 / 1.2, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, fh * 0.62))
        v, e = extrude_ring(bm, e, translate=(0, 0, fh * 0.16), scale=(0.93, 0.93, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, fh * 0.10), scale=(1.14, 1.14, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, fh * 0.06), scale=(0.98, 0.98, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, 0), scale=(0.74, 0.74, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, -fh * 0.22), scale=(0.96, 0.96, 1))
        bm.faces.new(v)
        tag(_sobj(bm, "Funnel", dark, bevel=min(0.02, fr * 0.15)), "Hull")
        band_z = fbase + fh * 0.80; band_h = fh * 0.10
        r_out = fr * 1.22; r_in = fr * 0.90
        bm = bmesh.new()
        v0, e0 = ngon_ring(bm, (ox, fy, band_z), r_out, 14, axis="Z")
        v, e = extrude_ring(bm, e0, translate=(0, 0, band_h))
        v, e = extrude_ring(bm, e, translate=(0, 0, 0), scale=(r_in / r_out, r_in / r_out, 1))
        v, e = extrude_ring(bm, e, translate=(0, 0, -band_h))
        bmesh.ops.bridge_loops(bm, edges=list(e) + list(e0))   # close the bottom annulus
        tag(_sobj(bm, "FunnelBand", red, bevel=0.004), "Hull")

    # rudder at the stern (+Y), below the waterline — a tapered NACA foil blade on a
    # round stock rising to deck level; steers (yaws) about the stock. Its top is
    # taken from the hull's own stern station so the blade tucks under the counter
    # instead of hanging in open water below it.
    rud_y = L * 0.5 + beam * 0.03
    if rudder:
        ry = rud_y
        rchord = max(0.08, beam * 0.22); rthick = max(0.02, beam * 0.05)
        _hbs, _dis, _rims = station(0.5)
        top_z = (_rims - _dis) - depth * 0.02
        bot_z = depth * 0.10
        rspan = top_z - bot_z
        bm = bmesh.new()
        v, e = _foil_ring(bm, (ox, ry, top_z), rchord, rthick, 12)
        bm.faces.new(v)
        for dz, sc in ((-rspan * 0.42, 1.0), (-rspan * 0.36, 0.90), (-rspan * 0.22, 0.62)):
            v, e = extrude_ring(bm, e, translate=(0, 0, dz), scale=(sc, sc, 1))
        bm.faces.new(v[::-1])
        tag(_sobj(bm, "Rudder", red, bevel=0.006), "RudderSteer", "steer")
        spar("RudderStock", (ox, ry - rchord * 0.28, top_z - rspan * 0.02),
             (ox, ry - rchord * 0.28, deck_z + max(0.08, depth * 0.10)),
             [(0.0, rthick * 0.55), (1.0, rthick * 0.42)], metal, "Z",
             bone="RudderSteer", role="steer", n=10)

    # motor propeller just ahead of the rudder, underwater — spins about forward.
    # Hub is a ring-loft boss with an ogive fairing; each blade is a twisted NACA
    # foil, rotated into place through the BMESH (verts are in world coords with the
    # object left at the origin, so rotating the finished object would pivot around
    # world (0,0,0) and fling the blade off — the bug the plane's props hit).
    prop_ty = 0.46
    _hbp, _dip, _rimp = station(prop_ty)
    hull_bot = _rimp - _dip                        # hull underside over the propeller
    hub_r = max(0.03, min(beam * 0.06, depth * 0.10))
    hub_l = max(0.10, min(beam * 0.24, L * 0.10))
    span = max(0.10, min(depth * 0.26, beam * 0.20))
    # hang the disc just clear of the hull instead of at a fixed depth, so the blade
    # tips nearly touch the counter at any hull_depth rather than floating below it
    prop_z = max(depth * 0.10, hull_bot - depth * 0.04 - hub_r * 0.80 - span)
    if prop:
        px = ox; py = L * prop_ty; pz = prop_z
        spar("PropHub", (px, py - hub_l * 0.45, pz), (px, py + hub_l * 0.55, pz),
             [(0.0, hub_r * 0.42), (0.16, hub_r), (0.52, hub_r), (0.80, hub_r * 0.70),
              (1.0, hub_r * 0.26)], metal, "Y", bone="Propeller", role="prop", n=12)
        # shaft up into the hull — without it the propeller reads as a detached part.
        # Kept short so it enters the hull near the counter instead of running
        # forward in open water to a station the hull bottom has not risen past yet.
        sh_y = py - hub_l * 1.3
        _hbs2, _dis2, _rims2 = station(sh_y / L)
        spar("PropShaft", (px, sh_y, (_rims2 - _dis2) + depth * 0.06),
             (px, py - hub_l * 0.30, pz),
             [(0.0, hub_r * 0.34), (1.0, hub_r * 0.42)], metal, "Y", n=10)
        chord = max(0.05, span * 0.52)
        root_z = pz + hub_r * 0.80
        for i in range(3):
            bm = bmesh.new()
            v, e = _foil_ring(bm, (px, py, root_z), chord, chord * 0.22, 12)
            bm.faces.new(v[::-1])
            bmesh.ops.rotate(bm, cent=(px, py, root_z),          # root pitch angle
                             matrix=mathutils.Matrix.Rotation(math.radians(34.0), 3, "Z"),
                             verts=v)
            for dz, sc, tw in ((span * 0.40, 1.10, -8.0), (span * 0.36, 0.90, -8.0),
                               (span * 0.24, 0.50, -6.0)):
                v, e = extrude_ring(bm, e, translate=(0, 0, dz), scale=(sc, sc, 1))
                cen = mathutils.Vector((0.0, 0.0, 0.0))
                for vv in v:
                    cen = cen + vv.co
                bmesh.ops.rotate(bm, cent=cen / len(v),          # wash the pitch out
                                 matrix=mathutils.Matrix.Rotation(math.radians(tw), 3, "Z"),
                                 verts=v)
            bm.faces.new(v)
            bmesh.ops.rotate(bm, cent=(px, py, pz),
                             matrix=mathutils.Matrix.Rotation(math.radians(i * 120.0), 3, "Y"),
                             verts=bm.verts[:])
            tag(_sobj(bm, "PropBlade_%d" % i, dark, bevel=0.002), "Propeller", "prop")

    # bone table for the rigid mechanical rigger
    global BOAT_BONES
    cz = depth * 0.5
    BOAT_BONES = [
        ("Root", (ox, 0, cz), (ox, 0, cz + 0.6), None),
        ("Hull", (ox, 0, cz), (ox, -L * 0.3, cz), "Root"),
    ]
    if rudder:
        BOAT_BONES.append(("RudderSteer", (ox, rud_y, depth * 0.42),
                           (ox, rud_y, depth * 0.42 + 0.4), "Hull"))
    if prop:
        BOAT_BONES.append(("Propeller", (ox, L * prop_ty, prop_z),
                           (ox, L * prop_ty - 0.3, prop_z), "Hull"))
    return parts
'''

_BUILDERS = {"plane": _PLANE_SRC, "boat": _BOAT_SRC,
             "humanoid": _HUMANOID_SRC, "bike": _BIKE_SRC,
             "furniture": _FURNITURE_SRC, "weapon": _WEAPON_SRC}

# ── riggers: define `rig_model(parts, ox)` per base (game-functional skeletons) ──
# Car: a root + body bone + one bone per wheel (axle along +X). Each part is rigidly
# skin-bound (single vertex group, weight 1.0) to its bone, so a wheel bone's roll
# spins that wheel and its Z move = suspension travel. Exports as a rigged FBX.
_BIKE_RIG = '''
def rig_model(parts, ox):
    """Bones from BIKE_BONES (built in build(), from the numbers that placed the
    geometry), binding still by ROLE. Positions used to be read off obj.location,
    which is (0,0,0) for every bmesh part in this file — verts are emitted in world
    coordinates with the object left at the origin (see loft()) — so both wheel
    bones landed on the world origin and VehicleBoneDriver's roll swung the wheels
    around it in a circle instead of spinning them on their axles."""
    import mathutils
    wheels = [o for o in parts if o.get("role") == "wheel"]
    if not wheels:
        return None
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm); _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones; made = {}
    for name, head, tail, parent in BIKE_BONES:
        b = eb.new(name)
        b.head = mathutils.Vector(head); b.tail = mathutils.Vector(tail)
        b.use_connect = False
        if parent and parent in made:
            b.parent = made[parent]
        made[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    valid = set(made)
    steer = "steer" if "steer" in valid else "frame"

    def bone_for(o):
        role = o.get("role")
        if role == "wheel":
            label = o.get("wheel_label", "")
            return label if label in valid else "frame"
        if role in ("fork", "handlebar"):
            return steer
        return "frame"

    def bind(obj, bone):
        obj.parent = rig
        vg = obj.vertex_groups.new(name=bone)
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig

    for o in parts:
        if o.type == "MESH":
            bind(o, bone_for(o))
    return rig
'''

# Humanoid: build the standard Unity HumanBodyBones skeleton (exact names so the
# importer's avatar auto-mapper recognises them), then rigidly skin each tagged
# mesh segment to its bone (weight 1.0). Required bones present: Hips, Spine, Head,
# {Left,Right}{UpperLeg,LowerLeg,Foot,UpperArm,LowerArm,Hand} -> a VALID Humanoid
# avatar, so any humanoid clip (Mixamo-style Run/Jump) retargets onto it.
_HUMANOID_RIG = '''
def rig_model(parts, ox):
    """Build the Unity HumanBodyBones skeleton from BONES_WORLD, then proximity-skin
    each part against ONLY its curated bone set (PART_BONES) — the humangen.py weight
    scheme. Curated maps keep arm weights off the torso etc., so joints deform cleanly
    and the rig imports as a valid Unity Humanoid avatar (no 'Hips not found')."""
    import mathutils
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm); _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones
    made = {}
    for name, head, tail, parent in BONES_WORLD:
        b = eb.new(name)
        b.head = mathutils.Vector(head)
        b.tail = mathutils.Vector(tail)
        b.use_connect = False
        if parent and parent in made:
            b.parent = made[parent]
        made[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")

    segs = {n: (mathutils.Vector(h), mathutils.Vector(t)) for (n, h, t, _) in BONES_WORLD}
    rad = WEIGHT_RADIUS

    def influence(co, head, tail):
        ab = tail - head
        denom = ab.dot(ab) or 1e-6
        u = max(0.0, min(1.0, (co - head).dot(ab) / denom))
        d = (co - (head + u * ab)).length
        return (1.0 - d / rad) if d < rad else 0.0

    def bind(obj):
        wmap = PART_BONES.get(obj.get("part"))
        if not wmap:
            bn = obj.get("bone")
            wmap = {bn: 1.0} if bn else None
        if not wmap:
            return
        obj.parent = rig
        groups = {}
        for v in obj.data.vertices:
            co = obj.matrix_world @ v.co
            ws = {}; tot = 0.0
            for bn, mx in wmap.items():
                if bn in segs:
                    w = influence(co, *segs[bn]) * mx
                    if w > 0.0:
                        ws[bn] = w; tot += w
            if tot <= 0.0:                       # outside every falloff -> nearest listed bone
                best = None; bd = 1e9
                for bn in wmap:
                    if bn in segs:
                        hh, tt = segs[bn]; ab = tt - hh; den = ab.dot(ab) or 1e-6
                        u = max(0.0, min(1.0, (co - hh).dot(ab) / den))
                        d = (co - (hh + u * ab)).length
                        if d < bd:
                            bd = d; best = bn
                if best:
                    ws = {best: 1.0}; tot = 1.0
            for bn, w in ws.items():
                groups.setdefault(bn, []).append((v.index, w / tot))
        for bn, items in groups.items():
            vg = obj.vertex_groups.new(name=bn)
            for vi, w in items:
                vg.add([vi], w, "REPLACE")
        m = obj.modifiers.new("arm", "ARMATURE"); m.object = rig

    for o in parts:
        if o.type == "MESH":
            bind(o)
    return rig
'''

# Plane: a rigid MECHANICAL rig (like the car) — build the bones from PLANE_BONES
# and rigidly skin each part to its bone (single group, weight 1.0). Propeller_* and
# Wheel.* bones are spun at Play by the name-driven VehicleBoneDriver.
_PLANE_RIG = '''
def rig_model(parts, ox):
    import mathutils
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm); _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones; made = {}
    for name, head, tail, parent in PLANE_BONES:
        b = eb.new(name)
        b.head = mathutils.Vector(head); b.tail = mathutils.Vector(tail)
        b.use_connect = False
        if parent and parent in made:
            b.parent = made[parent]
        made[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    valid = set(made)

    def bind(o, bone):
        if bone not in valid:
            bone = "Fuselage"
        o.parent = rig
        vg = o.vertex_groups.new(name=bone)
        vg.add([v.index for v in o.data.vertices], 1.0, "REPLACE")
        m = o.modifiers.new("arm", "ARMATURE"); m.object = rig

    for o in parts:
        if o.type == "MESH":
            bind(o, o.get("bone", "Fuselage"))
    return rig
'''

# Boat: rigid mechanical rig (like the plane) over BOAT_BONES. The rudder
# (RudderSteer) and propeller (Propeller) bones are spun/yawed by VehicleBoneDriver.
_BOAT_RIG = '''
def rig_model(parts, ox):
    import mathutils
    arm = bpy.data.armatures.new(COLL + "_arm")
    rig = bpy.data.objects.new(COLL + "_rig", arm); _into(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.edit_bones; made = {}
    for name, head, tail, parent in BOAT_BONES:
        b = eb.new(name)
        b.head = mathutils.Vector(head); b.tail = mathutils.Vector(tail)
        b.use_connect = False
        if parent and parent in made:
            b.parent = made[parent]
        made[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    valid = set(made)

    def bind(o, bone):
        if bone not in valid:
            bone = "Hull"
        o.parent = rig
        vg = o.vertex_groups.new(name=bone)
        vg.add([v.index for v in o.data.vertices], 1.0, "REPLACE")
        m = o.modifiers.new("arm", "ARMATURE"); m.object = rig

    for o in parts:
        if o.type == "MESH":
            bind(o, o.get("bone", "Hull"))
    return rig
'''

_NO_RIG = '''
def rig_model(parts, ox):
    return None
'''

_RIGGERS = {"bike": _BIKE_RIG, "humanoid": _HUMANOID_RIG,
            "plane": _PLANE_RIG, "boat": _BOAT_RIG}

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
        elif "prop" in pb.name.lower():
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, _m.radians(spin_deg * 1.5), 0.0)  # spin the propeller
        elif "steer" in pb.name.lower():
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, _m.radians(steer_deg), 0.0)  # yaw the steer/rudder bone
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

    Raises UnknownBaseError for a base with no builder here — it will not emit a
    different base's geometry under the requested name.
    """
    _require_base(base, _BUILDERS, "has no builder")
    header = (
        "BASE = {b!r}\nCOLL = {c!r}\nOX = {x}\nRIG = {r}\nDEMO = {dm}\nPARAMS = {p}\n".format(
            b=base, c=coll, x=float(ox), r=bool(rig), dm=bool(demo), p=json.dumps(params))
    )
    tail = ""
    if out_dir:
        header = '_OUT_DIR = r"{d}"\n'.format(d=str(out_dir).replace("\\", "/")) + header
        tail = _MANIFEST_TAIL
    rigger = _RIGGERS.get(base, _NO_RIG)
    return (header + _HELPERS + _PARTS + _MESH_TOOLKIT + "\n" + _BUILDERS[base] + "\n"
            + rigger + _DEMO + "\n" + _RUN + tail)
