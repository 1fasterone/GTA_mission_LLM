"""
Opt-in headless Blender build gate.

tests/test_base_models.py deliberately never executes real Blender — every
check there is compile()-on-source-text only (see that file's module
docstring). That catches Python syntax errors but nothing about the bmesh
calls themselves: a real bmesh.ops mismatch (wrong operator name, an op that
leaves a non-manifold hole) only shows up by actually running the script.

This module closes that gap for the edge-loop-sculpted bases: launches real
headless Blender against a generated build script (the same
find_blender + subprocess mechanism src/micro/model.py uses for the live
`unity-model` CLI) and asserts on the emitted result JSON plus mesh validity.
SLOW (a Blender process launch per case) and requires a local Blender
install, so it is gated behind its own marker, excluded from the default run
by pytest.ini's addopts.

Run with:    pytest -m blender_render
Skips when:  no Blender install is found (blender.blender_path in
             config.yaml, $BLENDER_PATH, or a standard install location).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

import vehicle_generator as vg
from src.blender.base_models import build_base_code, clamp_params
from src.blender.locator import find_blender

_TIMEOUT_S = 120


def _run_headless(code: str, tmp_path: Path, label: str) -> dict:
    script_path = tmp_path / f"{label}.run.py"
    script_path.write_text(code, encoding="utf-8")
    bl = find_blender()
    if bl is None:
        pytest.skip("No Blender install found — set blender.blender_path or $BLENDER_PATH.")
    cmd = [str(bl.path), "--background", "--factory-startup", "--python", str(script_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
    result_json = next(tmp_path.glob("*.result.json"), None)
    assert proc.returncode == 0 and result_json is not None, (
        f"headless Blender build failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    return json.loads(result_json.read_text(encoding="utf-8"))


# ── validation script tail: real bmesh manifold checks, not just "did it run" ──
_VALIDATE_TAIL = '''
_bad = []
for _o in _coll().objects:
    if _o.type != "MESH":
        continue
    import bmesh as _bm
    _b = _bm.new(); _b.from_mesh(_o.data)
    _nm = [e for e in _b.edges if not e.is_manifold]
    if _nm or _o.data.validate(verbose=False):
        _bad.append(_o.name)
    _b.free()
result["nonmanifold_or_invalid"] = _bad
import json as _json2, os as _os2
with open(_os2.path.join(_OUT_DIR, COLL + ".result.json"), "w", encoding="utf-8") as _f2:
    _json2.dump(result, _f2, indent=2)
'''


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw", [
    ("rifle", {}),
    ("sniper", {"barrel_len": 0.9, "scope": 1, "stock": 1}),
    ("extreme_min", {"length": 0.20, "caliber": 0.015, "barrel_len": 0.10,
                      "stock": 2, "muzzle": 1, "magazine": 2, "scope": 1,
                      "grip": 1, "foregrip": 1}),
    ("extreme_max", {"length": 1.40, "caliber": 0.08, "barrel_len": 0.95,
                      "stock": 2, "muzzle": 2, "magazine": 2, "scope": 1,
                      "grip": 1, "foregrip": 1}),
])
def test_weapon_builds_clean_in_real_blender(label, raw):
    """Every weapon part is a real, manifold, error-free mesh — not just a script
    that compiles. Regression cover for the bug this gate caught during
    development: notch()'s original extrude_face_region call left every
    grip/magazine panel with 4 open (non-manifold) edges, invisible to the
    compile-only test suite and only visible in a real build."""
    p = clamp_params("weapon", raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("weapon", p, coll=f"PROC_{label}",
                               out_dir=str(tmp_path))
        code += _VALIDATE_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["parts"] > 0
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh(es) in {label}: {result['nonmanifold_or_invalid']}"
    )


# plane's own rig contract (base_models._PLANE_RIG) binds by name via each part's
# "bone" custom property matching a PLANE_BONES entry — a typo'd/missing "bone"
# silently rigid-binds to "Fuselage" instead of erroring, so the compile-only
# tests can't catch it. This tail confirms every expected bone actually got a
# non-empty vertex group, not just that the build ran.
_PLANE_VALIDATE_TAIL = '''
_bad = []
for _o in _coll().objects:
    if _o.type != "MESH":
        continue
    import bmesh as _bm4
    _b = _bm4.new(); _b.from_mesh(_o.data)
    _nm = [e for e in _b.edges if not e.is_manifold]
    if _nm or _o.data.validate(verbose=False):
        _bad.append(_o.name)
    _b.free()
result["nonmanifold_or_invalid"] = _bad
_rig = [o for o in _coll().objects if o.type == "ARMATURE"]
_bones = {b.name for b in _rig[0].data.bones} if _rig else set()
_expected = {"Root", "Fuselage", "Propeller_R", "Propeller_L", "Wheel.F", "Wheel.RR", "Wheel.RL"}
result["missing_bones"] = sorted(_expected - _bones)
_empty_vg = []
for _o in _coll().objects:
    if _o.type == "MESH":
        for _vg in _o.vertex_groups:
            _n = sum(1 for _v in _o.data.vertices for _g in _v.groups
                     if _g.group == _vg.index and _g.weight > 0)
            if _n == 0:
                _empty_vg.append(_o.name + "/" + _vg.name)
result["empty_vertex_groups"] = _empty_vg
import json as _json4, os as _os4
with open(_os4.path.join(_OUT_DIR, COLL + ".result.json"), "w", encoding="utf-8") as _f4:
    _json4.dump(result, _f4, indent=2)
'''


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw", [
    ("default", {}),
    ("min", {"fuselage_len": 3.0, "fuselage_r": 0.30, "wing_span": 3.0,
             "wing_chord": 0.6, "fin_h": 0.5}),
    ("max", {"fuselage_len": 9.0, "fuselage_r": 1.00, "wing_span": 12.0,
             "wing_chord": 2.5, "fin_h": 2.0}),
])
def test_plane_builds_clean_in_real_blender(label, raw):
    """The live unity-model plane path (base_models._PLANE_SRC, NOT
    vehicle_generator.generate_plane — see test_vehicle_builds_clean_in_real_blender
    below for that separate, non-live path): real, manifold, error-free meshes,
    AND every one of the 7 PLANE_BONES still gets a matching, non-empty part.
    Regression cover for the bug this gate caught during development: rotating a
    finished PropBlade object (built with verts in world coords, object left at
    the origin — matches loft()'s documented convention) pivoted around world
    (0,0,0) instead of the hub, flinging 2 of 3 blades per side off into space;
    invisible to the compile-only test suite, obvious in a render."""
    p = clamp_params("plane", raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("plane", p, coll=f"PROC_{label}", rig=True, demo=True,
                               out_dir=str(tmp_path))
        code += _PLANE_VALIDATE_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["parts"] > 0
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh(es) in {label}: {result['nonmanifold_or_invalid']}"
    )
    assert result["missing_bones"] == [], f"missing PLANE_BONES in {label}: {result['missing_bones']}"
    assert result["empty_vertex_groups"] == [], (
        f"empty vertex group(s) (silent fallback-to-Fuselage?) in {label}: "
        f"{result['empty_vertex_groups']}"
    )


# boat's rig contract (base_models._BOAT_RIG) binds by name the same way plane's
# does — an unknown "bone" silently falls back to "Hull" rather than erroring — and
# its sculpted parts (bellied sail loft, hollow funnel + closed collar, foil rudder,
# twisted foil propeller blades) are exactly the shapes a compile-only test cannot
# check. Same tail as the plane's, with BOAT_BONES' expected set.
_BOAT_VALIDATE_TAIL = '''
_bad = []
for _o in _coll().objects:
    if _o.type != "MESH":
        continue
    import bmesh as _bm5
    _b = _bm5.new(); _b.from_mesh(_o.data)
    _nm = [e for e in _b.edges if not e.is_manifold]
    if _nm or _o.data.validate(verbose=False):
        _bad.append(_o.name)
    _b.free()
result["nonmanifold_or_invalid"] = _bad
_rig = [o for o in _coll().objects if o.type == "ARMATURE"]
_bones = {b.name for b in _rig[0].data.bones} if _rig else set()
result["missing_bones"] = sorted({n for n, _h, _t, _p in BOAT_BONES} - _bones)
_empty_vg = []
for _o in _coll().objects:
    if _o.type == "MESH":
        for _vg in _o.vertex_groups:
            _n = sum(1 for _v in _o.data.vertices for _g in _v.groups
                     if _g.group == _vg.index and _g.weight > 0)
            if _n == 0:
                _empty_vg.append(_o.name + "/" + _vg.name)
result["empty_vertex_groups"] = _empty_vg
result["part_names"] = sorted(o.name for o in _coll().objects if o.type == "MESH")
import json as _json5, os as _os5
with open(_os5.path.join(_OUT_DIR, COLL + ".result.json"), "w", encoding="utf-8") as _f5:
    _json5.dump(result, _f5, indent=2)
'''


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw,expect", [
    ("sailboat", {"masts": 1, "sails": 1, "prop": 0},
     ("Mast_0", "Sail_0_0", "Yard_0_0")),
    ("schooner", {"masts": 2, "sails": 1, "hull_len": 12.0, "mast_h": 7.0},
     ("Mast_1", "Sail_1_1", "Yard_1_1")),
    ("tug", {"masts": 0, "sails": 0, "prop": 1, "cabin": 1, "funnel": 1},
     ("Deckhouse", "Wheelhouse", "Funnel", "FunnelBand", "PropHub", "PropBlade_2")),
    ("rowboat", {"masts": 0, "sails": 0, "prop": 0, "rudder": 0}, ("Hull", "Deck")),
    ("extreme_min", {"hull_len": 3.0, "hull_beam": 1.0, "hull_depth": 0.6,
                     "deck_h": 0.05, "mast_h": 1.0, "masts": 3, "sails": 1,
                     "prop": 1, "cabin": 1, "rudder": 1, "funnel": 1},
     ("Rudder", "RudderStock", "PropBlade_0")),
    ("extreme_max", {"hull_len": 16.0, "hull_beam": 5.0, "hull_depth": 3.0,
                     "deck_h": 0.40, "mast_h": 9.0, "masts": 3, "sails": 1,
                     "prop": 1, "cabin": 1, "rudder": 1, "funnel": 1},
     ("Rudder", "RudderStock", "PropBlade_0")),
])
def test_boat_builds_clean_in_real_blender(label, raw, expect):
    """Every boat part is a real, manifold, error-free mesh and every BOAT_BONES
    entry still gets a matching, non-empty vertex group. Covers the AAA sculpting
    pass: the sail's hand-lofted closed cloth volume, the funnel's rolled rim +
    bridged collar annulus, the NACA foil sections (whose leading/trailing edges
    collapse into zero-length edges if sampled on whole steps), and the propeller
    blades' bmesh-space rotation about the hub."""
    p = clamp_params("boat", raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("boat", p, coll=f"PROC_{label}", rig=True, demo=True,
                               out_dir=str(tmp_path))
        code += _BOAT_VALIDATE_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["parts"] > 0
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh(es) in {label}: {result['nonmanifold_or_invalid']}"
    )
    assert result["missing_bones"] == [], f"missing BOAT_BONES in {label}: {result['missing_bones']}"
    assert result["empty_vertex_groups"] == [], (
        f"empty vertex group(s) (silent fallback-to-Hull?) in {label}: "
        f"{result['empty_vertex_groups']}"
    )
    missing = [n for n in expect if n not in result["part_names"]]
    assert missing == [], f"{label} lost sculpted part(s) {missing}: {result['part_names']}"


# bike's rigger binds by ROLE and places bones from BIKE_BONES. On top of the usual
# manifold/vertex-group checks this tail reports each wheel bone's head against its
# wheel's actual vertex centroid: the bones used to be positioned from obj.location,
# which is (0,0,0) for every bmesh part in base_models (world-coord verts, object at
# the origin), so rolling a wheel bone swung the wheel around the world origin.
_BIKE_VALIDATE_TAIL = '''
import mathutils as _mu6
_bad = []
for _o in _coll().objects:
    if _o.type != "MESH":
        continue
    import bmesh as _bm6
    _b = _bm6.new(); _b.from_mesh(_o.data)
    _nm = [e for e in _b.edges if not e.is_manifold]
    if _nm or _o.data.validate(verbose=False):
        _bad.append(_o.name)
    _b.free()
result["nonmanifold_or_invalid"] = _bad
_rig = [o for o in _coll().objects if o.type == "ARMATURE"]
_bones = {b.name: b for b in _rig[0].data.bones} if _rig else {}
result["missing_bones"] = sorted({n for n, _h, _t, _p in BIKE_BONES} - set(_bones))
_empty_vg = []
for _o in _coll().objects:
    if _o.type == "MESH":
        for _vg in _o.vertex_groups:
            _n = sum(1 for _v in _o.data.vertices for _g in _v.groups
                     if _g.group == _vg.index and _g.weight > 0)
            if _n == 0:
                _empty_vg.append(_o.name + "/" + _vg.name)
result["empty_vertex_groups"] = _empty_vg
_off = {}
for _o in _coll().objects:
    if _o.type != "MESH" or _o.get("role") != "wheel":
        continue
    _label = _o.get("wheel_label", "")
    _b = _bones.get(_label)
    if _b is None:
        continue
    _c = sum((_o.matrix_world @ _mu6.Vector(_v.co) for _v in _o.data.vertices),
             _mu6.Vector()) / max(1, len(_o.data.vertices))
    _off[_label] = round((_c - _b.head_local).length, 4)
result["wheel_bone_offsets"] = _off
result["part_names"] = sorted(o.name for o in _coll().objects if o.type == "MESH")
import json as _json6, os as _os6
with open(_os6.path.join(_OUT_DIR, COLL + ".result.json"), "w", encoding="utf-8") as _f6:
    _json6.dump(result, _f6, indent=2)
'''


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw,expect", [
    ("bicycle", {}, ("Wheel.F", "Wheel.R", "top_tube", "chain_stay_1", "seat",
                     "chainring", "crank_0", "handlebar", "grip_0")),
    ("motorbike", {"engine": 1, "tank": 1, "pedals": 0},
     ("engine", "cylinder", "exhaust", "tank")),
    ("dirtbike", {"engine": 1, "tank": 1, "pedals": 0, "suspension": 1,
                  "wheel_w": 0.20}, ("fork_0", "fork_1", "steerer")),
    ("unicycle", {"wheels": 1}, ("Wheel.C", "post", "seat")),
    ("extreme_min", {"wheelbase": 0.60, "wheel_r": 0.20, "wheel_w": 0.022,
                     "frame_h": 0.25, "handlebar_w": 0.30, "engine": 1, "tank": 1,
                     "suspension": 1, "pedals": 1}, ("engine", "tank")),
    ("extreme_max", {"wheelbase": 1.60, "wheel_r": 0.55, "wheel_w": 0.32,
                     "frame_h": 0.65, "handlebar_w": 0.80, "engine": 1, "tank": 1,
                     "suspension": 1, "pedals": 1}, ("engine", "tank")),
])
def test_bike_builds_clean_in_real_blender(label, raw, expect):
    """Every bike part is a real, manifold, error-free mesh, and each wheel bone sits
    ON its wheel rather than at the world origin — the rig defect this gate was
    written to catch, invisible to compile-only tests because the bones were placed
    from obj.location, which every bmesh part legitimately leaves at (0,0,0)."""
    p = clamp_params("bike", raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("bike", p, coll=f"PROC_{label}", rig=True, demo=True,
                               out_dir=str(tmp_path))
        code += _BIKE_VALIDATE_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["parts"] > 0
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh(es) in {label}: {result['nonmanifold_or_invalid']}"
    )
    assert result["missing_bones"] == [], f"missing BIKE_BONES in {label}: {result['missing_bones']}"
    assert result["empty_vertex_groups"] == [], (
        f"empty vertex group(s) in {label}: {result['empty_vertex_groups']}"
    )
    offsets = result["wheel_bone_offsets"]
    assert offsets, f"{label} bound no wheel to a wheel bone"
    for bone, dist in offsets.items():
        assert dist < 0.05, (
            f"{label}: bone {bone} is {dist} from its wheel's centre — rolling it "
            f"orbits the wheel instead of spinning it"
        )
    missing = [n for n in expect if n not in result["part_names"]]
    assert missing == [], f"{label} lost sculpted part(s) {missing}: {result['part_names']}"


# _VALIDATE_TAIL plus the emitted part names, for the un-rigged bases whose gate is
# "is every mesh real and did the sculpted parts all show up".
_PART_NAMES_TAIL = _VALIDATE_TAIL.replace(
    'import json as _json2, os as _os2',
    'result["part_names"] = sorted(o.name for o in _coll().objects if o.type == "MESH")\n'
    'import json as _json2, os as _os2')


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw,expect", [
    ("table", {}, ("top", "leg_0", "leg_0_block", "apron_y_1")),
    ("chair", {"seat": 1, "back": 1, "width": 0.45, "depth": 0.45, "height": 0.45},
     ("back", "back_stile_0", "back_rail")),
    ("sofa", {"seat": 1, "back": 1, "cushion": 1, "width": 1.9, "depth": 0.85,
              "height": 0.42},
     ("top", "back", "arm_0", "arm_1", "arm_roll_0", "back_roll",
      "cushion_0", "back_cushion_0")),
    ("bookcase", {"legs": 0, "shelves": 4, "height": 1.8, "depth": 0.32},
     ("side_0", "back_panel", "plinth", "shelf_3")),
    ("dresser", {"legs": 0, "drawers": 4, "height": 1.1},
     ("Drawer.0", "Drawer.3", "knob_0", "body")),
    ("desk", {"drawers": 3, "width": 1.4}, ("pedestal", "Drawer.2", "leg_3")),
    ("pedestal_table", {"legs": 1, "width": 0.6, "depth": 0.6}, ("column", "foot")),
    ("extreme_min", {"width": 0.30, "depth": 0.25, "height": 0.30, "top_t": 0.02,
                     "leg_r": 0.02, "legs": 3, "shelves": 6, "drawers": 6},
     ("top", "leg_0")),
    ("extreme_max", {"width": 3.00, "depth": 2.20, "height": 2.20, "top_t": 0.20,
                     "leg_r": 0.18, "legs": 4, "seat": 1, "back": 1, "cushion": 1,
                     "drawers": 6}, ("top", "back")),
])
def test_furniture_builds_clean_in_real_blender(label, raw, expect):
    """Every furniture part is a real, manifold, error-free mesh across the whole
    knob matrix — the turned legs' profile-of-revolution loft, the moulded slab
    edges, the notched carcass panels and drawer fields, and the puffy cushion with
    its stitched seam."""
    p = clamp_params("furniture", raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("furniture", p, coll=f"PROC_{label}",
                               out_dir=str(tmp_path))
        code += _PART_NAMES_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["parts"] > 0
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh(es) in {label}: {result['nonmanifold_or_invalid']}"
    )
    missing = [n for n in expect if n not in result["part_names"]]
    assert missing == [], f"{label} lost sculpted part(s) {missing}: {result['part_names']}"


# _VALIDATE_TAIL plus each part's WORLD bounding box. The defects below are all
# "the mesh is perfectly valid, it is just the wrong size / in the wrong place",
# which every other gate in this file passes straight over: manifold checks,
# part-name checks and bone-offset checks are all blind to a part that quietly
# grew 12% and swallowed its neighbour.
_BOUNDS_TAIL = _VALIDATE_TAIL.replace(
    'import json as _json2, os as _os2',
    'import mathutils as _mu7\n'
    'bpy.context.view_layer.update()   # matrix_world is stale after location edits\n'
    '_bx = {}\n'
    'for _o in _coll().objects:\n'
    '    if _o.type != "MESH":\n'
    '        continue\n'
    '    _wc = [_o.matrix_world @ _mu7.Vector(_c) for _c in _o.bound_box]\n'
    '    _bx[_o.name] = [[min(_v[_i] for _v in _wc) for _i in range(3)],\n'
    '                    [max(_v[_i] for _v in _wc) for _i in range(3)]]\n'
    'result["bounds"] = _bx\n'
    'result["part_names"] = sorted(_bx)\n'
    'import json as _json2, os as _os2')


def _span(bounds, name, axis):
    lo, hi = bounds[name]
    return lo[axis], hi[axis]


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw", [
    ("sofa", {"seat": 1, "back": 1, "cushion": 1, "width": 2.0, "depth": 0.90,
              "height": 0.45}),
    ("armchair", {"seat": 1, "back": 1, "cushion": 1, "width": 0.85, "depth": 0.85,
                  "height": 0.42}),
    ("extreme_max", {"seat": 1, "back": 1, "cushion": 1, "width": 3.00,
                     "depth": 2.20, "height": 2.20, "top_t": 0.20}),
])
def test_upholstered_cushions_never_overlap_the_arms(label, raw):
    """An upholstered piece is a sofa only if the arms are BESIDE the cushions.

    Regression cover for two defects a manifold/part-name gate cannot see:

    1. slab(puffy=True) applied its two bulge rings cumulatively (1.05 * 1.07),
       so every pad came out 12.35% wider than the caller asked for. Each end
       cushion grew 60 mm into the arm next to it and hid it completely.
    2. the seat cushions spanned the full width rather than the width BETWEEN
       the arms, so the arms sat on top of the cushions instead of flanking them.

    Both produced a perfectly valid mesh that simply did not read as a sofa.
    """
    p = clamp_params("furniture", raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("furniture", p, coll=f"PROC_{label}",
                               out_dir=str(tmp_path))
        code += _BOUNDS_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh(es) in {label}: {result['nonmanifold_or_invalid']}"
    )
    bounds = result["bounds"]
    arms = [n for n in bounds if n.startswith("arm_") and "roll" not in n]
    cushions = [n for n in bounds if n.startswith(("cushion_", "back_cushion_"))]
    assert len(arms) == 2, f"{label} built {len(arms)} arm panels, expected 2"
    assert cushions, f"{label} built no cushions"

    tol = 0.002       # 2 mm of shared skin is fine; a cushion inside an arm is not
    for arm in arms:
        a0, a1 = _span(bounds, arm, 0)
        for cush in cushions:
            c0, c1 = _span(bounds, cush, 0)
            overlap = min(a1, c1) - max(a0, c0)
            assert overlap <= tol, (
                f"{label}: {cush} overlaps {arm} by {overlap * 1000:.0f} mm in X — "
                f"the cushion is buried in the arm, so the piece reads as a padded "
                f"bench rather than a sofa"
            )

    # and no arm may bulge outboard of the back panel, which spans the full width
    b0, b1 = _span(bounds, "back", 0)
    for arm in arms:
        a0, a1 = _span(bounds, arm, 0)
        assert a0 >= b0 - tol and a1 <= b1 + tol, (
            f"{label}: {arm} spans x[{a0:.3f} {a1:.3f}] but the back panel spans "
            f"x[{b0:.3f} {b1:.3f}] — the arm sticks out past the piece"
        )


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw", [
    ("road", {"wheel_w": 0.022}),
    ("default_bicycle", {}),
    ("motorbike", {"wheel_w": 0.18, "engine": 1, "tank": 1, "pedals": 0}),
    ("chopper", {"wheel_w": 0.32, "wheel_r": 0.55, "engine": 1, "tank": 1, "pedals": 0}),
])
def test_wheel_tread_stays_inside_its_own_envelope(label, raw):
    """A tyre's tread may not push past the wheel's nominal radius or width.

    notch()'s `inset` is an ABSOLUTE distance (inset_individual's thickness), but
    _wheel_ring passed a flat 0.10 as though it were a fraction. On anything
    narrower than a 200 mm tyre the inset exceeded the whole face, inset_individual
    inverted, and the grooves came out as lugs standing PROUD of the tread — every
    bicycle looked like a fatbike, and the tyre's true lowest point moved, which
    silently breaks the `wheel_z = wheel_r` axle-height assumption make_wheel's
    docstring depends on for the wheel to sit flush on the ground.
    """
    p = clamp_params("bike", raw)
    wr, ww = p["wheel_r"], p["wheel_w"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = build_base_code("bike", p, coll=f"PROC_{label}", out_dir=str(tmp_path))
        code += _BOUNDS_TAIL
        result = _run_headless(code, tmp_path, label)
    bounds = result["bounds"]
    wheels = [n for n in bounds if n.startswith("Wheel.")]
    assert wheels, f"{label} built no wheels: {result['part_names']}"
    for wheel in wheels:
        z0, z1 = _span(bounds, wheel, 2)
        assert z1 - z0 <= wr * 2.0 + 0.002, (
            f"{label}: {wheel} is {z1 - z0:.4f} tall but its nominal diameter is "
            f"{wr * 2.0:.4f} — the tread is proud of the rolling radius, so the "
            f"wheel no longer sits flush on the ground"
        )
        x0, x1 = _span(bounds, wheel, 0)
        assert x1 - x0 <= ww * 1.12 + 0.002, (
            f"{label}: {wheel} is {x1 - x0:.4f} wide but wheel_w is {ww:.4f} "
            f"(hub adds 8%) — the tread has blown out sideways into lugs"
        )


# vehicle_generator's detailed builder writes ONE object (`obj`), not a named
# collection iterated via _coll() — a different validate tail than base_models'.
_VG_VALIDATE_TAIL = '''
import bmesh as _bm3
_b = _bm3.new(); _b.from_mesh(obj.data)
_nm = [e for e in _b.edges if not e.is_manifold]
result["nonmanifold_or_invalid"] = [obj.name] if (_nm or obj.data.validate(verbose=False)) else []
_b.free()
import json as _json3, os as _os3
with open(_os3.path.join(_OUT_DIR, COLL + ".result.json"), "w", encoding="utf-8") as _f3:
    _json3.dump(result, _f3, indent=2)
'''


@pytest.mark.blender_render
@pytest.mark.parametrize("kind,label,raw", [
    ("car", "sedan", {}),
    ("car", "suv", {"body_h": 1.05, "wheel_r": 0.60, "cabin_h": 0.80, "body_len": 4.6}),
    ("car", "extreme_wheel", {"wheel_r": 0.95, "wheel_w": 0.62, "rim": 0.72}),
    ("plane", "cessna", {}),
    ("plane", "jet", {"jets": 1}),
])
def test_vehicle_builds_clean_in_real_blender(kind, label, raw):
    """The live unity-model car/plane path (vehicle_generator.py), not just the
    wrapper-script text: real, manifold, error-free meshes and (for car) all
    four wheels still classified/skin-bound correctly after add_wheel grew real
    tread/hub geometry — regression cover for the add_strut NameError this gate
    caught during development (a stray `return res` left over from the original
    single-cylinder implementation, invisible to the compile-only test suite)."""
    clamp = vg.clamp_plane_params if kind == "plane" else vg.clamp_params
    p = clamp(raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = vg.build_detailed_code(p, coll=f"PROC_{label}", name=label,
                                      seed_str=label, rig=True, demo=True,
                                      kind=kind, out_dir=str(tmp_path))
        code += _VG_VALIDATE_TAIL
        result = _run_headless(code, tmp_path, label)
    assert result["rigged"] is True
    assert result["nonmanifold_or_invalid"] == [], (
        f"non-manifold/invalid mesh in {kind}/{label}: {result['nonmanifold_or_invalid']}"
    )


# The car is ONE welded mesh — wheels, lights and body share it, and which part
# you are looking at is decided purely by each face's material_index. That makes a
# missed material assignment invisible to every geometry check in this file: the
# mesh stays manifold, the wheel stays exactly where it belongs, and it renders in
# body paint. This tail reports material slots by WORLD position so the tests below
# can assert what each region is actually made of.
_VG_MATERIAL_TAIL = '''
import mathutils as _mu9
bpy.context.view_layer.update()
_slots = [m.name if m else "" for m in obj.data.materials]
_ix = {n: i for i, n in enumerate(_slots)}
_half_w = PARAMS["body_w"] * 0.5
_half_l = PARAMS["body_len"] * 0.5
_outboard = {}
_side = {}
for _p in obj.data.polygons:
    _c = obj.matrix_world @ _p.center
    _name = _slots[_p.material_index] if _p.material_index < len(_slots) else "?"
    if abs(_c.x) > _half_w + 0.02:                      # only wheels reach out here
        _outboard[_name] = _outboard.get(_name, 0) + 1
    if abs(_c.x) > _half_w * 0.88 and abs(_c.y) < _half_l * 0.60:   # door flank
        _side[_name] = _side.get(_name, 0) + 1
result["material_slots"] = _slots
result["outboard_face_materials"] = _outboard
result["flank_face_materials"] = _side
import json as _json9, os as _os9
with open(_os9.path.join(_OUT_DIR, COLL + ".result.json"), "w", encoding="utf-8") as _f9:
    _json9.dump(result, _f9, indent=2)
'''


@pytest.mark.blender_render
@pytest.mark.parametrize("label,raw", [
    ("sedan", {}),
    ("suv", {"body_h": 1.05, "wheel_r": 0.60, "cabin_h": 0.80, "body_len": 4.6}),
    ("extreme_wheel", {"wheel_r": 0.95, "wheel_w": 0.62, "rim": 0.72}),
])
def test_car_wheels_are_rubber_not_bodywork(label, raw):
    """Every face out at the wheels is tyre or hub — never body paint.

    add_wheel assigned Material.tire only to the tread band it collected for
    grooving, so each tyre's two end caps (one big n-gon covering the whole disc)
    kept material slot 0 and rendered in BODY PAINT. All four wheels came out
    body-coloured with a thin black rim, on a mesh that passes every manifold,
    bone-offset and part-name check in this file.

    The flank assertion covers the second half of the same class of defect:
    recessed_grid recolours every cell it creates, and the side face IS the whole
    door, so asking it for Material.trim turned the entire flank black on a coin
    flip of the random seed.
    """
    p = vg.clamp_params(raw)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = vg.build_detailed_code(p, coll=f"PROC_{label}", name=label,
                                      seed_str=label, kind="car",
                                      out_dir=str(tmp_path))
        code += _VG_MATERIAL_TAIL
        result = _run_headless(code, tmp_path, label)
    outboard = result["outboard_face_materials"]
    assert outboard, f"{label}: no faces found outboard of the body at all"
    assert outboard.get("veh_paint", 0) == 0, (
        f"{label}: {outboard['veh_paint']} body-paint face(s) out at the wheels — "
        f"the tyre sidewalls are painted the body colour. Got {outboard}"
    )
    assert outboard.get("veh_tire", 0) > 0, f"{label}: no rubber at the wheels: {outboard}"

    flank = result["flank_face_materials"]
    assert flank.get("veh_trim", 0) == 0, (
        f"{label}: {flank['veh_trim']} trim face(s) on the door flank — the side "
        f"character line has repainted the whole panel black. Got {flank}"
    )
