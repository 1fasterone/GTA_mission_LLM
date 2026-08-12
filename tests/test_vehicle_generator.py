"""
Tests for vehicle_generator.py (the live car/plane backend — src/micro/model.py's
_VG_VARIANTS routes "car" here, not through src/blender/base_models.py). Pure
host-side: param clamping, keyword routing, emitted-wrapper-script validity
(compile-checked, not executed). No Blender / no LLM.

This module had zero prior test coverage despite being the generator the live
`unity-model car` CLI path actually uses (base_models._CAR_SRC, which WAS
tested, was dead code — see docs/PROCEDURAL_BASES.md). The real geometry
functions (generate_vehicle/generate_plane/add_wheel/...) need bpy and can only
be exercised for real in tests/test_blender_render.py's opt-in headless gate.
"""

from __future__ import annotations

import pytest

import vehicle_generator as vg


# ── param clamping ────────────────────────────────────────────────────────────

def test_clamp_fills_defaults_and_color():
    p = vg.clamp_params({})
    for k, (default, lo, hi) in vg.PARAM_SCHEMA.items():
        assert p[k] == default


def test_clamp_clamps_out_of_range():
    p = vg.clamp_params({"wheel_r": 99.0, "body_w": -5})
    lo_r, hi_r = vg.PARAM_SCHEMA["wheel_r"][1], vg.PARAM_SCHEMA["wheel_r"][2]
    lo_w = vg.PARAM_SCHEMA["body_w"][1]
    assert p["wheel_r"] == hi_r
    assert p["body_w"] == lo_w


def test_plane_clamp_fills_defaults():
    p = vg.clamp_plane_params({})
    for k, (default, lo, hi) in vg.PLANE_PARAM_SCHEMA.items():
        assert p[k] == default


# ── keyword routing (no-LLM fallback) ─────────────────────────────────────────

def test_keyword_vehicle_params_big_tires():
    h = vg.keyword_vehicle_params("a lifted offroad jeep")
    assert h["wheel_r"] >= 0.7


def test_keyword_plane_params_jet():
    h = vg.keyword_plane_params("a fighter jet")
    assert h.get("jets") == 1


# ── emitted wrapper script (compile-checked; the real geometry is bpy-only) ────

@pytest.mark.parametrize("kind,params", [
    ("car", {}),
    ("car", {"body_h": 1.05, "wheel_r": 0.80, "wheel_w": 0.55}),   # suv-ish
    ("car", {"wheel_r": 0.95, "wheel_w": 0.62, "rim": 0.72}),      # extreme wheel
    ("plane", {}),
    ("plane", {"jets": 1}),
])
def test_build_detailed_code_compiles(kind, params):
    clamp = vg.clamp_plane_params if kind == "plane" else vg.clamp_params
    p = clamp(params)
    code = vg.build_detailed_code(p, coll="PROC_t", name="t", seed_str="t",
                                  rig=True, demo=True, kind=kind)
    compile(code, f"<{kind}>", "exec")
    assert "import vehicle_generator as _vg" in code
    assert f'KIND = {kind!r}' in code


def test_build_detailed_code_tail_when_out_dir():
    p = vg.clamp_params({})
    code = vg.build_detailed_code(p, coll="PROC_t", name="t", seed_str="t",
                                  out_dir="/tmp/x", kind="car")
    assert "result.json" in code and "_OUT_DIR" in code
    compile(code, "<car_tail>", "exec")
