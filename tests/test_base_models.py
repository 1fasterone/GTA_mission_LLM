"""
Tests for the parametric base-model pipeline (src/blender/base_models.py +
procedural_pipeline.map_prompt). Pure host-side: param clamping, keyword routing,
emitted-script validity (compile-checked, not executed). No Blender / no LLM.
"""

import json

import pytest

from src.blender.base_models import (
    BASE_NAMES, BASE_PARAM_SCHEMA, build_base_code, clamp_params,
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


def test_non_car_base_gets_noop_rigger():
    code = build_base_code("plane", clamp_params("plane", {}), coll="PROC_plane", rig=True)
    compile(code, "<plane_rig>", "exec")
    assert "def rig_model(parts, ox):\n    return None" in code   # no-op rig for now


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
