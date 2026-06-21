"""
Tests for the parametric base-model pipeline (src/blender/base_models.py +
procedural_pipeline.map_prompt). Pure host-side: param clamping, keyword routing,
emitted-script validity (compile-checked, not executed). No Blender / no LLM.
"""

import json

import pytest

from src.blender.base_models import (
    BASE_NAMES, BASE_PARAM_SCHEMA, HUMANOID_PALETTE, build_base_code, clamp_params,
    keyword_base, keyword_hints,
)
from procedural_pipeline import map_prompt


# ── param clamping ────────────────────────────────────────────────────────────

def test_clamp_fills_defaults_and_color():
    p = clamp_params("car", {})
    for k, (default, lo, hi) in BASE_PARAM_SCHEMA["car"].items():
        assert p[k] == default
    assert p["color"] == pytest.approx((0.12, 0.32, 0.72))


def test_clamp_clamps_out_of_range_and_bad_values():
    p = clamp_params("car", {"wheel_r": 99.0, "body_w": -5, "ride": "huge"})
    assert p["wheel_r"] == 0.95            # clamped to max
    assert p["body_w"] == 1.2              # clamped to min
    assert p["ride"] == 0.0                # non-numeric -> default


def test_clamp_drops_unknown_keys_and_bad_base():
    p = clamp_params("nonsense", {"bogus": 1, "wheel_r": 0.5})
    assert "bogus" not in p
    assert set(p) == set(BASE_PARAM_SCHEMA["car"]) | {"color"}   # bad base -> car


def test_clamp_color_validation():
    # 0..1 values clamp; a 0-255 triple (max>1) is normalised to 0..1
    assert clamp_params("car", {"color": [0.2, -1, 0.5]})["color"] == pytest.approx((0.2, 0.0, 0.5))
    assert clamp_params("car", {"color": [255, 0, 128]})["color"] == pytest.approx((1.0, 0.0, 128 / 255))
    assert clamp_params("car", {"color": "red"})["color"] == pytest.approx((0.12, 0.32, 0.72))


# ── keyword routing (no-LLM fallback) ─────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("a rugged jeep", "car"),
    ("an old biplane", "plane"),
    ("a tall robot guard", "humanoid"),
    ("a glowing crystal", None),
])
def test_keyword_base(prompt, expected):
    assert keyword_base(prompt) == expected


def test_keyword_hints_big_tires():
    h = keyword_hints("jeep with big tires", "car")
    assert h["wheel_r"] >= 0.7 and h["ride"] > 0.0


def test_keyword_hints_tall_thin():
    h = keyword_hints("a tall thin android", "humanoid")
    assert h["height"] >= 2.0 and h["shoulder_w"] <= 0.4


# ── emitted bpy script (compile-checked, every base) ──────────────────────────

@pytest.mark.parametrize("base", BASE_NAMES)
def test_build_base_code_compiles(base):
    params = clamp_params(base, {})
    code = build_base_code(base, params, coll=f"PROC_{base}", ox=3.0)
    compile(code, f"<{base}>", "exec")                 # valid Python
    assert "def build(p, ox):" in code
    assert "primitive_" in code and "def colour" in code
    assert f'BASE = {base!r}' in code
    assert json.dumps(params) in code                  # params embedded verbatim


def test_build_base_code_tail_when_out_dir():
    code = build_base_code("car", clamp_params("car", {}), coll="PROC_car", out_dir="/tmp/x")
    assert "result.json" in code and "_OUT_DIR" in code
    compile(code, "<car_tail>", "exec")


# ── end-to-end mapping (fallback path, no LLM) ────────────────────────────────

def test_map_prompt_fallback_jeep():
    m = map_prompt("a jeep with big tires", use_llm=False)
    assert m["source"] == "fallback" and m["base"] == "car"
    assert m["params"]["wheel_r"] >= 0.7
    # the plan must produce a compilable script
    code = build_base_code(m["base"], m["params"], coll="PROC_test")
    compile(code, "<map>", "exec")


def test_map_prompt_fallback_plane_and_humanoid():
    assert map_prompt("a small plane", use_llm=False)["base"] == "plane"
    assert map_prompt("a humanoid figure", use_llm=False)["base"] == "humanoid"


# ── structural car knobs (limo != stretched jeep) ─────────────────────────────

def test_car_structural_knobs_clamp_and_compile():
    p = clamp_params("car", {"bed": 1, "wheel_pairs": 3, "cabin_len": 0.78, "cabin_pos": 0.9})
    assert p["bed"] == 1.0
    assert p["wheel_pairs"] == 3.0
    assert p["cabin_len"] == 0.78
    assert p["cabin_pos"] == 0.30                  # clamped to max
    compile(build_base_code("car", p, coll="PROC_pickup"), "<pickup>", "exec")


def test_car_defaults_include_structural_knobs():
    p = clamp_params("car", {})
    for k in ("cabin_len", "cabin_pos", "bed", "wheel_pairs", "rim"):
        assert k in p


# ── part-library: rig + semantic names ────────────────────────────────────────

def test_car_rig_code_compiles_and_has_parts_and_rigger():
    code = build_base_code("car", clamp_params("car", {}), coll="PROC_car", rig=True, demo=True)
    compile(code, "<car_rig>", "exec")
    assert "def make_wheel(" in code            # part builder present
    assert "Wheel." in code                     # semantic wheel naming
    assert "def rig_model(parts, ox):" in code  # rigger present
    assert "def demo_pose(" in code             # demo present
    assert "ARMATURE" in code                   # skin-bind


def test_static_base_gets_noop_rigger():
    # weapon is still a static prop -> no-op rigger
    code = build_base_code("weapon", clamp_params("weapon", {}), coll="PROC_weapon", rig=True)
    compile(code, "<weapon_rig>", "exec")
    assert "def rig_model(parts, ox):\n    return None" in code   # static prop, no rig


def test_plane_emit_ports_planegen_and_mechanical_rig():
    code = build_base_code("plane", clamp_params("plane", {}),
                           coll="PROC_plane", rig=True, demo=True)
    compile(code, "<plane_rig>", "exec")
    # recognisable plane parts (fuselage shell, engines, props, gear)
    for part in ('"Fuselage"', '"Nose"', '"Cockpit"', '"Wing_R"', '"V_Stabilizer"',
                 "Engine_", "PropBlade_", "Wheel.F"):
        assert part in code, part
    # name-driven bone roles the VehicleBoneDriver spins (prop + wheels)
    assert "Propeller_" in code and '"prop"' in code
    # rigid mechanical rigger (weight 1.0 single bind), not the no-op
    assert "PLANE_BONES" in code and "1.0" in code
    assert "def rig_model(parts, ox):\n    return None" not in code


# ── bike base (part reuse: make_wheel + wheel-bone rig) ───────────────────────

def test_bike_routing_and_hints():
    assert keyword_base("a red dirt bike") == "bike"
    assert keyword_base("a motorcycle") == "bike"
    assert keyword_base("a unicycle") == "bike"
    dirt = keyword_hints("an off-road dirt bike", "bike")
    assert dirt["suspension"] == 1.0 and dirt["engine"] == 1.0 and dirt["pedals"] == 0.0
    assert keyword_hints("a unicycle", "bike")["wheels"] == 1.0


def test_bike_builds_and_rigs_compile():
    p = clamp_params("bike", {"engine": 1, "tank": 1, "pedals": 0, "suspension": 1})
    code = build_base_code("bike", p, coll="PROC_bike", rig=True, demo=True)
    compile(code, "<bike_rig>", "exec")
    assert "def rig_model(parts, ox):" in code and "steer" in code
    assert "make_wheel(" in code and "tube_between(" in code
    # unicycle variant also compiles
    uni = clamp_params("bike", {"wheels": 1})
    compile(build_base_code("bike", uni, coll="PROC_uni", rig=True), "<uni>", "exec")


def test_bike_map_prompt_fallback():
    m = map_prompt("a dirt bike", use_llm=False)
    assert m["base"] == "bike" and m["params"]["suspension"] == 1.0


# ── furniture base (slab + leg composition; static no-op rigger) ───────────────

def test_furniture_routing_and_hints():
    assert keyword_base("a wooden dining table") == "furniture"
    assert keyword_base("an office chair") == "furniture"
    assert keyword_base("a tall bookcase") == "furniture"
    assert keyword_base("a 6-drawer dresser") == "furniture"
    chair = keyword_hints("a wooden chair", "furniture")
    assert chair["seat"] == 1.0 and chair["back"] == 1.0
    shelf = keyword_hints("a bookcase", "furniture")
    assert shelf["legs"] == 0 and shelf["shelves"] >= 1
    dresser = keyword_hints("a dresser", "furniture")
    assert dresser["drawers"] >= 1 and dresser["legs"] == 0


def test_furniture_clamp_includes_structural_knobs():
    p = clamp_params("furniture", {})
    for k in ("width", "depth", "height", "top_t", "leg_r", "legs",
              "seat", "back", "shelves", "drawers", "cushion"):
        assert k in p
    assert p["color"] == pytest.approx((0.45, 0.30, 0.16))


def test_furniture_builds_compile_across_kinds():
    # table (legged), bookcase (carcass + shelves), dresser (drawers),
    # chair (seat+back), pedestal (1 leg) — each must emit valid Python.
    for label, raw in [
        ("table",    {}),
        ("bookcase", {"legs": 0, "shelves": 4, "height": 1.8}),
        ("dresser",  {"legs": 0, "drawers": 5}),
        ("desk",     {"legs": 4, "drawers": 3}),
        ("chair",    {"seat": 1, "back": 1, "height": 0.46}),
        ("pedestal", {"legs": 1}),
    ]:
        p = clamp_params("furniture", raw)
        code = build_base_code("furniture", p, coll=f"PROC_{label}", rig=True, demo=True)
        compile(code, f"<{label}>", "exec")
        assert "def build(p, ox):" in code


def test_furniture_gets_noop_rigger():
    code = build_base_code("furniture", clamp_params("furniture", {}),
                           coll="PROC_furniture", rig=True)
    compile(code, "<furniture_rig>", "exec")
    assert "def rig_model(parts, ox):\n    return None" in code   # static decor


def test_furniture_map_prompt_fallback():
    m = map_prompt("a wooden bookshelf", use_llm=False)
    assert m["base"] == "furniture" and m["params"]["shelves"] >= 1
    compile(build_base_code(m["base"], m["params"], coll="PROC_shelf"), "<shelf>", "exec")


# ── weapon base (receiver + barrel + attachment slots; static no-op rigger) ────

def test_weapon_routing_and_hints():
    assert keyword_base("a chrome pistol") == "weapon"
    assert keyword_base("a suppressed sniper rifle") == "weapon"
    assert keyword_base("a drum-fed smg") == "weapon"
    pistol = keyword_hints("a pistol", "weapon")
    assert pistol["stock"] == 0 and pistol["barrel_len"] <= 0.2
    sniper = keyword_hints("a suppressed sniper rifle", "weapon")
    assert sniper["scope"] == 1 and sniper["muzzle"] == 2 and sniper["barrel_len"] >= 0.8
    assert keyword_hints("an smg with a drum mag", "weapon")["magazine"] == 2


def test_weapon_clamp_includes_attachment_knobs():
    p = clamp_params("weapon", {})
    for k in ("length", "caliber", "barrel_len", "stock", "muzzle",
              "magazine", "scope", "grip", "foregrip"):
        assert k in p
    assert p["color"] == pytest.approx((0.15, 0.15, 0.16))
    # structural knobs clamp to their integer range
    over = clamp_params("weapon", {"stock": 9, "muzzle": -3, "magazine": 5})
    assert over["stock"] == 2 and over["muzzle"] == 0 and over["magazine"] == 2


def test_weapon_builds_compile_across_loadouts():
    for label, raw in [
        ("rifle",      {}),
        ("pistol",     {"stock": 0, "barrel_len": 0.14, "length": 0.32}),
        ("sniper",     {"barrel_len": 0.9, "scope": 1, "stock": 1}),
        ("suppressed", {"muzzle": 2, "foregrip": 1}),
        ("drum_smg",   {"magazine": 2, "stock": 2, "length": 0.6}),
        ("revolver",   {"stock": 0, "magazine": 0, "grip": 1}),
    ]:
        p = clamp_params("weapon", raw)
        code = build_base_code("weapon", p, coll=f"PROC_{label}", rig=True, demo=True)
        compile(code, f"<{label}>", "exec")
        assert "def build(p, ox):" in code


def test_weapon_gets_noop_rigger():
    code = build_base_code("weapon", clamp_params("weapon", {}),
                           coll="PROC_weapon", rig=True)
    compile(code, "<weapon_rig>", "exec")
    assert "def rig_model(parts, ox):\n    return None" in code   # static prop


def test_weapon_map_prompt_fallback():
    m = map_prompt("a scoped sniper rifle", use_llm=False)
    assert m["base"] == "weapon" and m["params"]["scope"] == 1.0
    compile(build_base_code(m["base"], m["params"], coll="PROC_sniper"), "<sniper>", "exec")


# ── humanoid: ring-loft toolkit, palette, proximity-weighted Unity rig ──────────

def test_humanoid_palette_clamp_and_default():
    p = clamp_params("humanoid", {})
    assert set(p["palette"]) == set(HUMANOID_PALETTE)
    # skin defaults to the model colour (so colour cues still tint the skin)
    assert p["palette"]["skin"] == pytest.approx(p["color"])
    # overrides are colour-validated: 0-255 normalised, out-of-range clamped
    q = clamp_params("humanoid", {"palette": {"shirt": [255, 0, 0], "pants": [0.2, -1, 0.5]}})
    assert q["palette"]["shirt"] == pytest.approx((1.0, 0.0, 0.0))
    assert q["palette"]["pants"] == pytest.approx((0.2, 0.0, 0.5))
    # a bad palette value falls back to the authored default
    assert clamp_params("humanoid", {"palette": {"hair": "brown"}})["palette"]["hair"] == \
        pytest.approx(HUMANOID_PALETTE["hair"])


def test_humanoid_emit_uses_ringloft_toolkit_and_proximity_rig():
    code = build_base_code("humanoid", clamp_params("humanoid", {}),
                           coll="PROC_h", rig=True, demo=True)
    compile(code, "<h>", "exec")
    # the smooth-mesh toolkit is present (not just stacked primitives)
    for fn in ("def loft(", "def finish(", "def uvsphere_bm(",
               "def collider_hull(", "def make_lods(", "def uv_unwrap("):
        assert fn in code
    # Unity naming conventions baked into the toolkit
    assert "COL_" in code and "LOD0_" in code
    # curated proximity skinning (smooth joints), not a rigid single weight-1.0 bind
    assert "PART_BONES" in code and "def influence(" in code
    # multi-material palette is consumed by the builder
    assert 'pal.get("shirt"' in code and 'pal.get("skin"' in code


def test_humanoid_rig_emits_required_unity_humanbodybones():
    code = build_base_code("humanoid", clamp_params("humanoid", {}),
                           coll="PROC_h", rig=True)
    # spine chain is emitted by exact name; limbs are built as `pre + "<suffix>"`
    # with pre in ("Left", "Right"), so assert the suffix tokens + both prefixes.
    for bone in ("Hips", "Spine", "Chest", "UpperChest", "Neck", "Head"):
        assert ('"%s"' % bone) in code, bone
    for suffix in ("UpperLeg", "LowerLeg", "Foot", "UpperArm", "LowerArm", "Hand"):
        assert ('"%s"' % suffix) in code, suffix
    assert '"Left"' in code and '"Right"' in code


# ── boat base (lofted hull + structural slots; mechanical rudder/prop rig) ──────

def test_boat_routing_and_hints():
    assert keyword_base("a small sailboat") == "boat"
    assert keyword_base("a red tugboat") == "boat"
    assert keyword_base("a wooden rowboat") == "boat"
    assert keyword_base("a motorboat") == "boat"
    sail = keyword_hints("a two-masted schooner", "boat")
    assert sail["sails"] == 1.0 and sail["masts"] >= 2 and sail["prop"] == 0.0
    motor = keyword_hints("a fast speedboat", "boat")
    assert motor["prop"] == 1.0 and motor["sails"] == 0.0
    tug = keyword_hints("a harbour tugboat", "boat")
    assert tug["funnel"] == 1.0 and tug["prop"] == 1.0
    row = keyword_hints("a little rowboat", "boat")
    assert row["masts"] == 0 and row["sails"] == 0.0


def test_boat_clamp_includes_structural_knobs():
    p = clamp_params("boat", {})
    for k in ("hull_len", "hull_beam", "hull_depth", "deck_h", "mast_h",
              "masts", "sails", "prop", "cabin", "rudder", "funnel"):
        assert k in p
    assert p["color"] == pytest.approx((0.32, 0.20, 0.12))
    over = clamp_params("boat", {"masts": 9, "hull_beam": 99})
    assert over["masts"] == 3 and over["hull_beam"] == 5.0


def test_boat_builds_compile_across_kinds():
    for label, raw in [
        ("sailboat",  {"masts": 1, "sails": 1, "prop": 0}),
        ("schooner",  {"masts": 2, "sails": 1}),
        ("motorboat", {"masts": 0, "sails": 0, "prop": 1, "cabin": 1}),
        ("tug",       {"prop": 1, "cabin": 1, "funnel": 1}),
        ("rowboat",   {"masts": 0, "sails": 0, "prop": 0, "rudder": 0}),
    ]:
        p = clamp_params("boat", raw)
        code = build_base_code("boat", p, coll=f"PROC_{label}", rig=True, demo=True)
        compile(code, f"<{label}>", "exec")
        assert "def build(p, ox):" in code


def test_boat_emit_has_mechanical_rig_and_driver_names():
    code = build_base_code("boat", clamp_params("boat", {"prop": 1}),
                           coll="PROC_boat", rig=True, demo=True)
    compile(code, "<boat_rig>", "exec")
    assert "BOAT_BONES" in code and '"Hull"' in code
    # name-driven roles: rudder yaws (steer), propeller spins (prop)
    assert "RudderSteer" in code and "Propeller" in code
    assert "def rig_model(parts, ox):\n    return None" not in code


def test_boat_map_prompt_fallback():
    m = map_prompt("a wooden sailboat", use_llm=False)
    assert m["base"] == "boat" and m["params"]["sails"] == 1.0
    compile(build_base_code(m["base"], m["params"], coll="PROC_sail"), "<sail>", "exec")


def test_model_wrapper_gates_unity_pipeline_to_humanoid():
    from src.micro.model import _build_base_code_with_fbx
    h = _build_base_code_with_fbx("humanoid", clamp_params("humanoid", {}),
                                  "MODEL_H", "out", "out/H.fbx", rig=True)
    compile(h, "<hw>", "exec")
    assert "collider_hull(COLL" in h and "make_lods(_parts" in h
    # other bases are untouched this pass (no LOD/collider post-pass)
    b = _build_base_code_with_fbx("bike", clamp_params("bike", {}),
                                  "MODEL_B", "out", "out/B.fbx", rig=True)
    compile(b, "<bw>", "exec")
    assert "make_lods(_parts" not in b and "collider_hull(COLL" not in b
