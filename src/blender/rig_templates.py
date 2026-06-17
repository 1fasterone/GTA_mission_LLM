"""
Authored rig-template armature library (Micro-Generator Phase 3 content).

The rig router (`rigging.py`) prefers a `RigTemplate<Class>V1` micro when an authored
armature ships for that class, else falls back to a deterministic auto micro. This
module IS that authored content — but kept as the project keeps everything: as
deterministic **data + code**, not opaque hand-saved binaries.

  * `TEMPLATE_BONES` — per class, a list of `(name, head, tail, parent)` bones in a
    NORMALISED space (≈2 units tall, base at z=0, +X = the character's left). Stable,
    game-ready, Unity-Humanoid-mappable names so animation can retarget by bone name.
  * `build_baker_code(out_dir)` — emits a self-contained bpy script that builds each
    armature from its spec and saves `<class>.blend` (one armature per file). Run it
    headless (`blender --background`) or in the live MCP Blender; the `.blend`s are
    reproducible artifacts of this data.
  * `FIT_BIND_SRC` — the bpy used by the emitted job script's `_append_template_armature`:
    append the authored armature, scale it to the mesh (uniform by height), seat it at
    the mesh's base, and bind with automatic weights. Deterministic given mesh+armature.

Bump `RIG_TEMPLATE_VERSION` (and the `RigTemplate*V<n>` ids in `rigging.py`) when the
bone layout changes so the cache invalidates correctly.
"""

from __future__ import annotations

import json
from typing import Optional

RIG_TEMPLATE_VERSION = 1

# A bone = (name, head(x,y,z), tail(x,y,z), parent|None). Normalised: ~2 tall, base z=0.
BoneSpec = tuple

# Helper to mirror an .L bone list to .R (negate X).
def _mirror(bones: list) -> list:
    out = list(bones)
    for name, h, t, p in bones:
        if name.endswith(".L"):
            rn = name[:-2] + ".R"
            rp = (p[:-2] + ".R") if (p and p.endswith(".L")) else p
            out.append((rn, (-h[0], h[1], h[2]), (-t[0], t[1], t[2]), rp))
    return out


_HUMANOID = _mirror([
    ("Hips",       (0.0, 0.0, 1.00), (0.0, 0.0, 1.15), None),
    ("Spine",      (0.0, 0.0, 1.15), (0.0, 0.0, 1.35), "Hips"),
    ("Chest",      (0.0, 0.0, 1.35), (0.0, 0.0, 1.55), "Spine"),
    ("Neck",       (0.0, 0.0, 1.55), (0.0, 0.0, 1.70), "Chest"),
    ("Head",       (0.0, 0.0, 1.70), (0.0, 0.0, 2.00), "Neck"),
    ("Shoulder.L", (0.05, 0.0, 1.50), (0.20, 0.0, 1.50), "Chest"),
    ("UpperArm.L", (0.20, 0.0, 1.50), (0.45, 0.0, 1.45), "Shoulder.L"),
    ("LowerArm.L", (0.45, 0.0, 1.45), (0.68, 0.0, 1.42), "UpperArm.L"),
    ("Hand.L",     (0.68, 0.0, 1.42), (0.82, 0.0, 1.41), "LowerArm.L"),
    ("UpperLeg.L", (0.10, 0.0, 1.00), (0.12, 0.0, 0.52), "Hips"),
    ("LowerLeg.L", (0.12, 0.0, 0.52), (0.13, 0.0, 0.10), "UpperLeg.L"),
    ("Foot.L",     (0.13, 0.0, 0.10), (0.14, -0.18, 0.00), "LowerLeg.L"),
])

_QUADRUPED = _mirror([
    ("Hips",          (0.0, 0.40, 0.70), (0.0, 0.10, 0.72), None),
    ("Spine",         (0.0, 0.10, 0.72), (0.0, -0.30, 0.74), "Hips"),
    ("Chest",         (0.0, -0.30, 0.74), (0.0, -0.60, 0.72), "Spine"),
    ("Neck",          (0.0, -0.60, 0.72), (0.0, -0.80, 0.85), "Chest"),
    ("Head",          (0.0, -0.80, 0.85), (0.0, -1.00, 0.95), "Neck"),
    ("Tail",          (0.0, 0.40, 0.70), (0.0, 0.80, 0.60), "Hips"),
    ("FrontUpperLeg.L", (0.20, -0.50, 0.70), (0.22, -0.50, 0.38), "Chest"),
    ("FrontLowerLeg.L", (0.22, -0.50, 0.38), (0.23, -0.50, 0.05), "FrontUpperLeg.L"),
    ("FrontFoot.L",     (0.23, -0.50, 0.05), (0.23, -0.62, 0.00), "FrontLowerLeg.L"),
    ("BackUpperLeg.L",  (0.20, 0.35, 0.68), (0.22, 0.35, 0.36), "Hips"),
    ("BackLowerLeg.L",  (0.22, 0.35, 0.36), (0.23, 0.35, 0.05), "BackUpperLeg.L"),
    ("BackFoot.L",      (0.23, 0.35, 0.05), (0.23, 0.22, 0.00), "BackLowerLeg.L"),
])

_CAR = [
    ("root",      (0.0, 0.0, 0.00), (0.0, 0.0, 0.20), None),
    ("body",      (0.0, 0.0, 0.20), (0.0, 0.0, 0.45), "root"),
    ("steering",  (0.0, -0.45, 0.45), (0.0, -0.45, 0.65), "body"),
    ("Wheel.FL",  (0.45, -0.55, 0.20), (0.62, -0.55, 0.20), "body"),
    ("Wheel.FR",  (-0.45, -0.55, 0.20), (-0.62, -0.55, 0.20), "body"),
    ("Wheel.RL",  (0.45, 0.55, 0.20), (0.62, 0.55, 0.20), "body"),
    ("Wheel.RR",  (-0.45, 0.55, 0.20), (-0.62, 0.55, 0.20), "body"),
]

_RIFLE = [
    ("root",     (0.0, 0.0, 0.0), (0.0, 0.30, 0.0), None),
    ("body",     (0.0, 0.30, 0.0), (0.0, -0.40, 0.0), "root"),
    ("Trigger",  (0.0, 0.05, -0.05), (0.0, 0.05, -0.15), "body"),
    ("Magazine", (0.0, 0.12, -0.05), (0.0, 0.12, -0.30), "body"),
    ("Bolt",     (0.0, 0.0, 0.05), (0.0, 0.18, 0.05), "body"),
]

_DOOR = [
    ("Frame", (0.0, 0.0, 0.0), (0.0, 0.0, 0.30), None),
    # Panel hinged at the frame edge (x=0), swinging across +X.
    ("Panel", (0.0, 0.0, 1.0), (0.60, 0.0, 1.0), "Frame"),
]

_CRATE = [
    ("root", (0.0, 0.0, 0.0), (0.0, 0.0, 0.30), None),
    # Lid hinged at the back-top edge (y=+0.5, z=top), opening upward/back.
    ("Lid",  (0.0, 0.50, 1.0), (0.0, -0.20, 1.0), "root"),
]

TEMPLATE_BONES: dict[str, list] = {
    "humanoid":  _HUMANOID,
    "quadruped": _QUADRUPED,
    "car":       _CAR,
    "rifle":     _RIFLE,
    "door":      _DOOR,
    "crate":     _CRATE,
}


def bone_names(template_class: str) -> list[str]:
    return [b[0] for b in TEMPLATE_BONES.get(template_class, [])]


def template_blend_name(template_class: str) -> str:
    return f"{template_class}.blend"


# ── baker: build each armature from its spec and save one .blend per class ──────

def build_baker_code(out_dir: str, classes: Optional[list[str]] = None) -> str:
    """Self-contained bpy script that bakes the requested template armatures
    (default: all) into `<out_dir>/<class>.blend`, one armature per file."""
    classes = classes or list(TEMPLATE_BONES.keys())
    data = {c: TEMPLATE_BONES[c] for c in classes}
    out_abs = out_dir.replace("\\", "/")
    return _BAKER.format(bones_json=json.dumps(data), out_dir=out_abs,
                         version=RIG_TEMPLATE_VERSION)


_BAKER = '''
import bpy, os, json

BONES = json.loads({bones_json!r})
OUT_DIR = r"{out_dir}"
VERSION = {version}
os.makedirs(OUT_DIR, exist_ok=True)
saved = []

def _purge(name):
    a = bpy.data.objects.get(name)
    if a is not None:
        bpy.data.objects.remove(a, do_unlink=True)
    ad = bpy.data.armatures.get(name)
    if ad is not None:
        bpy.data.armatures.remove(ad)

for cls, bones in BONES.items():
    arm_name = "RigTemplate_" + cls
    _purge(arm_name)
    arm_data = bpy.data.armatures.new(arm_name)
    arm_obj = bpy.data.objects.new(arm_name, arm_data)
    bpy.context.scene.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones
    created = {{}}
    # first pass: create all bones (head/tail); second: parent (specs are ordered
    # parent-first, but be safe).
    for name, h, t, p in bones:
        b = eb.new(name)
        b.head = tuple(h)
        b.tail = tuple(t)
        created[name] = b
    for name, h, t, p in bones:
        if p and p in created:
            created[name].parent = created[p]
            created[name].use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    arm_obj["rig_template"] = cls
    arm_obj["rig_template_version"] = VERSION
    path = os.path.join(OUT_DIR, cls + ".blend")
    # write ONLY this armature object to its own .blend (clean, appendable)
    bpy.data.libraries.write(path, {{arm_obj}}, fake_user=True, compress=True)
    saved.append({{"class": cls, "path": path.replace("\\\\\\\\", "/"),
                  "bones": len(bones), "names": [b[0] for b in bones]}})
    # clean the scene copy so the next class starts fresh
    _purge(arm_name)

result = {{"saved": saved, "count": len(saved), "version": VERSION}}
print("[rig-templates] baked " + str(len(saved)) + " armatures -> " + OUT_DIR)
'''


# ── append + deterministic fit + bind (injected into the emitted job script) ────
# Exposed as a string so mcp_backend can inject it; `RIG_TEMPLATE_BLEND` is a
# format field of the job template (the authored armature path for this job).
FIT_BIND_SRC = '''
def _append_template_armature(obj, strat):
    """Append the authored armature for this template class, fit it to the mesh
    (uniform scale by height, seated at the mesh base), and bind with automatic
    weights. Deterministic; returns the armature object or None to fall back."""
    if not RIG_TEMPLATE_BLEND or not os.path.exists(RIG_TEMPLATE_BLEND):
        warnings.append("template armature missing (" + strat + ") -> auto fallback")
        return None
    try:
        with bpy.data.libraries.load(RIG_TEMPLATE_BLEND, link=False) as (src, dst):
            dst.objects = [n for n in src.objects]
        appended = [o for o in dst.objects if o is not None and o.type == "ARMATURE"]
    except Exception as ex:
        warnings.append("template append failed: " + str(ex))
        return None
    if not appended:
        warnings.append("template .blend had no armature -> auto fallback")
        return None
    rig_obj = appended[0]
    coll = _collection()
    for c in list(rig_obj.users_collection):
        c.objects.unlink(rig_obj)
    coll.objects.link(rig_obj)
    rig_obj.name = obj.name + "_rig"
    # Fit: uniform scale so the armature height matches the mesh, seated at base.
    bpy.context.view_layer.update()
    arm_h = max(1e-4, rig_obj.dimensions.z)
    mesh_h = max(1e-4, obj.dimensions.z)
    s = mesh_h / arm_h
    rig_obj.scale = (s, s, s)
    # mesh bbox min (world) for seating
    import mathutils as _mu
    zmin = min((obj.matrix_world @ _mu.Vector(c)).z for c in obj.bound_box)
    rig_obj.location = (obj.location.x, obj.location.y, zmin)
    bpy.context.view_layer.objects.active = rig_obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    # Bind with automatic weights.
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    rig_obj.select_set(True)
    bpy.context.view_layer.objects.active = rig_obj
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as ex:
        warnings.append("template bind failed: " + str(ex))
    operations.append("template_rig:" + strat)
    return rig_obj
'''
