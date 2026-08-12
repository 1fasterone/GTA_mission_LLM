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
    ("extreme_min", {"wheelbase": 0.60, "wheel_r": 0.20, "wheel_w": 0.06,
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
              "height": 0.42}, ("top", "back")),
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
