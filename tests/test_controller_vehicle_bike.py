"""vehicle_bike regression coverage.

Two things had to both be true for a motocross/motorcycle brief to actually
produce a two-wheeled rig, and neither was, before this test existed:

  1. src/unity_compose.py's locomotion routing had to pick the `vehicle_bike`
     controller variant instead of always defaulting `locomotion == "drive"`
     to `vehicle_car` (the schema declared the variant; nothing ever selected
     it — see is_hover for the precedent this mirrors).
  2. templates/unity/micro/controller/Controller.cs.j2 had to actually
     implement vehicle_bike differently from vehicle_car (a 2-wheel rig with
     visual lean) rather than silently falling into the generic
     `variant.startswith("vehicle_")` branch shared with cars.

Live evidence this was broken: a full e2e run on a brief entirely about "a
motocross bike... lean angle... without steering wheels" produced a 4-wheel
WheelCollider car with a handbrake and zero occurrences of "lean" anywhere in
the emitted script.
"""
from __future__ import annotations

from pathlib import Path

from src.csharp_syntax_gate import check_files
from src.micro.controller import ControllerPipeline, ControllerSchema
from src.unity_compose import subsystems_for

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    """Pipeline ctor needs LMStudio config, but this test never calls the LLM.

    config.yaml is gitignored (machine-specific — see .gitignore) and does
    not exist in a fresh CI checkout; a real read here failed every test in
    this file in CI while passing locally. Mirrors the `dummy_config`
    fixture used the same way across tests/test_micro_generators.py and
    friends, inlined here since this file has only one pipeline to build.
    """
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":  str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


def _controller_variant(**kw) -> str:
    subs = subsystems_for("3D", [], locomotion="drive", genre="racing", **kw)
    ctrl = next(s for s in subs if s.kind == "controller")
    return ctrl.variant


# ── Routing (src/unity_compose.py) ──────────────────────────────────────────

def test_motocross_prompt_routes_to_vehicle_bike():
    prompt = ("A motocross bike races a dirt circuit; the player leans into "
              "corners without steering wheels.")
    assert _controller_variant(user_prompt=prompt) == "vehicle_bike"


def test_plain_car_prompt_still_routes_to_vehicle_car():
    prompt = "A street racing game with tuner cars drifting through downtown."
    assert _controller_variant(user_prompt=prompt) == "vehicle_car"


def test_drive_with_no_prompt_defaults_to_vehicle_car():
    assert _controller_variant(user_prompt="") == "vehicle_car"


def test_hover_words_still_win_their_own_lane():
    prompt = "A hovercar speeder race above a neon canyon."
    assert _controller_variant(user_prompt=prompt) == "vehicle_hover"


# ── Template (Controller.cs.j2) ─────────────────────────────────────────────

def _render(variant: str) -> str:
    pipe = ControllerPipeline(_cfg())
    schema = pipe.default_fix(
        ControllerSchema(name="TestCtrl", variant=variant, abilities=["nitro"]),
        variant,
    )
    env = pipe.jinja()
    return env.get_template("Controller.cs.j2").render(s=schema, variant=variant)


def test_vehicle_bike_gets_a_two_wheel_rig_and_lean():
    out = _render("vehicle_bike")
    assert "new WheelCollider[2]" in out
    assert "maxLeanAngle" in out
    assert "_leanPivot" in out
    assert "FreezeRotationX" in out and "FreezeRotationZ" in out
    assert out.count("{") == out.count("}")


def test_vehicle_car_is_unaffected():
    out = _render("vehicle_car")
    assert "new WheelCollider[4]" in out
    assert "maxLeanAngle" not in out
    assert "_leanPivot" not in out
    assert out.count("{") == out.count("}")


def test_both_variants_pass_the_real_csharp_syntax_gate(tmp_path):
    car_path = tmp_path / "TestCtrlCar.cs"
    bike_path = tmp_path / "TestCtrlBike.cs"
    car_path.write_text(_render("vehicle_car"), encoding="utf-8")
    bike_path.write_text(_render("vehicle_bike"), encoding="utf-8")
    result = check_files([str(car_path), str(bike_path)])
    if result.skipped_reason:
        return  # no .NET SDK on this machine — same no-op every other syntax-gate test allows
    assert not result.diagnostics, result.diagnostics
