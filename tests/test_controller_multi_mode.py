"""Control Surface Stack (docs/CONTROL_SURFACE_STACK.md) — Phase 1 coverage.

Phase 1 ships exactly two mode kinds (on_foot, vehicle_car) behind a NEW
ControllerVariant, "multi_mode", that carries an internal `modes` registry
instead of the usual one-variant-per-project rule. The hard constraint this
suite locks in: everything here is strictly additive — a project that never
sets `locomotion_modes` must route/render/compose byte-identically to before
this feature existed (see test_locomotion_modes_below_threshold_takes_old_path
and the golden/vehicle_bike suites, which this file does not touch).
"""
from __future__ import annotations

import yaml

from src.csharp_syntax_gate import check_files
from src.micro.controller import ControllerMode, ControllerPipeline, ControllerSchema
from src.unity_compose import subsystems_for
from src.unity_playability import ERROR, WARN, evaluate
from src.unity_schema import EntitySpec, UnitySchema, UnityScript


def _cfg() -> dict:
    return yaml.safe_load(open("config.yaml", encoding="utf-8"))


def _multi_schema(modes, **kw) -> ControllerSchema:
    return ControllerSchema(
        name="TestCtrl", variant="multi_mode",
        modes=[ControllerMode(**m) for m in modes],
        **kw)


def _fixed(schema: ControllerSchema) -> ControllerSchema:
    return ControllerPipeline(_cfg()).default_fix(schema, "multi_mode")


def _render(schema: ControllerSchema) -> str:
    pipe = ControllerPipeline(_cfg())
    fixed = pipe.default_fix(schema, "multi_mode")
    env = pipe.jinja()
    return env.get_template("ControllerMultiMode.cs.j2").render(s=fixed, variant="multi_mode")


def _unity_schema(locomotion_modes=None, entities=None, scripts=None, **kw) -> UnitySchema:
    return UnitySchema(
        project_name="MultiModeTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        locomotion_modes=locomotion_modes or [],
        entities=entities if entities is not None else [],
        scripts=scripts or [],
        **kw)


def _playable_avatar() -> dict:
    return {
        "entities": [EntitySpec(name="Hero", kind="player", count=1, tag="Player")],
        "scripts": [UnityScript(name="PlayerController", type="player", description="d"),
                    UnityScript(name="CamRig", type="camera", description="d")],
    }


# ── Schema / fixer (src/micro/controller.py) ────────────────────────────────

class TestFixer:
    def test_prunes_abilities_per_mode(self):
        fixed = _fixed(_multi_schema([
            {"kind": "on_foot", "abilities": ["jump", "sprint", "nitro"]},
            {"kind": "vehicle_car", "abilities": ["jump", "nitro"]},
        ]))
        on_foot = next(m for m in fixed.modes if m.kind == "on_foot")
        vehicle = next(m for m in fixed.modes if m.kind == "vehicle_car")
        assert set(on_foot.abilities) == {"jump", "sprint"}
        assert set(vehicle.abilities) == {"nitro"}

    def test_default_mode_falls_back_when_invalid(self):
        fixed = _fixed(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "vehicle_car"}],
            default_mode="not_a_registered_mode",
        ))
        assert fixed.default_mode == "on_foot"
        assert "default_mode_fallback" in fixed.meta["multi_mode"]

    def test_default_mode_kept_when_valid(self):
        fixed = _fixed(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "vehicle_car"}],
            default_mode="vehicle_car",
        ))
        assert fixed.default_mode == "vehicle_car"
        assert "multi_mode" not in fixed.meta

    def test_dedupes_by_kind(self):
        fixed = _fixed(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "on_foot"}, {"kind": "vehicle_car"}]))
        assert [m.kind for m in fixed.modes] == ["on_foot", "vehicle_car"]
        assert fixed.meta["multi_mode"]["dropped_duplicate_kinds"] == ["on_foot"]

    def test_mode_switch_key_falls_back_when_unusable(self):
        fixed = _fixed(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "vehicle_car"}],
            mode_switch_key="LeftControl",   # named differently in KeyCode vs. Key
        ))
        assert fixed.mode_switch_key == "Tab"

    def test_prunes_flying_abilities_to_flaps_and_landing_gear_only(self):
        fixed = _fixed(_multi_schema([
            {"kind": "on_foot"},
            {"kind": "flying", "abilities": ["flaps", "landing_gear", "nitro", "jump"]},
        ]))
        flying = next(m for m in fixed.modes if m.kind == "flying")
        assert set(flying.abilities) == {"flaps", "landing_gear"}

    def test_flaps_and_landing_gear_stripped_outside_multi_mode(self):
        # No template section for them anywhere but ControllerMultiMode.cs.j2's
        # flying macros — surviving into a single-variant schema.abilities
        # would be a silent gap.
        pipe = ControllerPipeline(_cfg())
        schema = ControllerSchema(name="X", variant="aircraft",
                                  abilities=["flaps", "landing_gear", "pitch"])
        fixed = pipe.default_fix(schema, "aircraft")
        assert "flaps" not in fixed.abilities
        assert "landing_gear" not in fixed.abilities


# ── Template (ControllerMultiMode.cs.j2) ─────────────────────────────────────

class TestRender:
    def test_renders_both_backing_stores_and_mode_api(self):
        out = _render(_multi_schema(
            [{"kind": "on_foot", "abilities": ["jump", "sprint"]},
             {"kind": "vehicle_car", "abilities": ["nitro"]}]))
        assert "CharacterController _cc" in out
        assert "Rigidbody _rb" in out
        assert "public void SwitchMode(string mode)" in out
        assert "public void CycleMode()" in out
        assert "public string CurrentMode" in out
        assert out.count("{") == out.count("}")

    def test_implements_ivehiclestate_when_vehicle_family_present(self):
        out = _render(_multi_schema([{"kind": "on_foot"}, {"kind": "vehicle_car"}]))
        assert ", IVehicleState" in out

    def test_single_mode_registry_renders_only_that_mode(self):
        # Not reachable through subsystems_for (which gates at 2+), but
        # ControllerPipeline itself imposes no minimum — this is what proves
        # the has_vehicle_car=False branch actually omits the vehicle rig
        # rather than silently emitting it unconditioned.
        out = _render(_multi_schema([{"kind": "on_foot", "abilities": ["jump"]}]))
        assert "CharacterController _cc" in out
        assert "Rigidbody _rb" not in out
        assert ", IVehicleState" not in out
        assert out.count("{") == out.count("}")

    def test_nitro_only_renders_when_a_mode_requests_it(self):
        with_nitro = _render(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "vehicle_car", "abilities": ["nitro"]}]))
        without_nitro = _render(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "vehicle_car"}]))
        assert "NitroRoutine" in with_nitro
        assert "NitroRoutine" not in without_nitro

    def test_passes_the_real_csharp_syntax_gate(self, tmp_path):
        out = _render(_multi_schema(
            [{"kind": "on_foot", "abilities": ["jump", "sprint"]},
             {"kind": "vehicle_car", "abilities": ["nitro"]}]))
        path = tmp_path / "TestCtrlMultiMode.cs"
        path.write_text(out, encoding="utf-8")
        result = check_files([str(path)])
        if result.skipped_reason:
            return  # no .NET SDK on this machine
        assert not result.diagnostics, result.diagnostics

    def test_flying_renders_its_own_rigidbody_rig_not_ivehiclestate(self):
        # flying drives a Rigidbody like vehicle_car does, but it is not a
        # wheeled vehicle — IVehicleState (SteerNormalized/WheelRadius/...)
        # stays vehicle_car-only.
        out = _render(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "flying"}]))
        assert "Rigidbody _rb" in out
        assert ", IVehicleState" not in out
        assert "public const string ModeFlying" in out
        assert "float pitchInput" in out
        assert "public float RudderNormalized" in out
        assert out.count("{") == out.count("}")

    def test_flaps_and_landing_gear_only_render_when_requested(self):
        bare = _render(_multi_schema([{"kind": "on_foot"}, {"kind": "flying"}]))
        assert "_flapsDown" not in bare
        assert "_gearDown" not in bare
        assert "FlapsDown" not in bare
        assert "GearDown" not in bare

        full = _render(_multi_schema(
            [{"kind": "on_foot"},
             {"kind": "flying", "abilities": ["flaps", "landing_gear"]}]))
        assert "_flapsDown" in full
        assert "_gearDown" in full
        assert "public bool  FlapsDown" in full
        assert "public bool  GearDown" in full
        assert full.count("{") == full.count("}")

    def test_vehicle_car_and_flying_share_one_rigidbody_no_double_mass(self):
        # Both modes drive the same shared _rb — flying_awake must defer mass
        # to vehicle_car's 1200f agreement with the physics micro rather than
        # stomping it with its own 2000f.
        out = _render(_multi_schema(
            [{"kind": "vehicle_car"}, {"kind": "flying"}]))
        assert out.count("private Rigidbody _rb;") == 1
        assert "_rb.mass" in out and "1200f" in out
        assert "2000f" not in out
        assert out.count("{") == out.count("}")

    def test_flying_alone_with_on_foot_sets_its_own_mass(self):
        out = _render(_multi_schema([{"kind": "on_foot"}, {"kind": "flying"}]))
        assert "_rb.mass = 2000f;" in out

    def test_three_way_registry_renders_all_three_dispatch_branches(self):
        out = _render(_multi_schema(
            [{"kind": "on_foot"}, {"kind": "vehicle_car"}, {"kind": "flying"}]))
        assert "if (_currentMode == ModeOnFoot)" in out
        assert "else if (_currentMode == ModeVehicleCar)" in out
        assert "else if (_currentMode == ModeFlying)" in out
        assert out.count("{") == out.count("}")

    def test_passes_the_real_csharp_syntax_gate_with_flying(self, tmp_path):
        out = _render(_multi_schema(
            [{"kind": "on_foot", "abilities": ["jump", "sprint"]},
             {"kind": "vehicle_car", "abilities": ["nitro"]},
             {"kind": "flying", "abilities": ["flaps", "landing_gear"]}]))
        path = tmp_path / "TestCtrlMultiModeFlying.cs"
        path.write_text(out, encoding="utf-8")
        result = check_files([str(path)])
        if result.skipped_reason:
            return  # no .NET SDK on this machine
        assert not result.diagnostics, result.diagnostics


# ── Routing (src/unity_compose.py subsystems_for) ────────────────────────────

class TestRouting:
    def _controller(self, **kw):
        subs = subsystems_for("3D", [], **kw)
        return next(s for s in subs if s.kind == "controller"), subs

    def test_below_threshold_takes_the_old_path(self):
        ctrl_default, _ = self._controller(locomotion="walk")
        ctrl_one_mode, _ = self._controller(locomotion="walk", locomotion_modes=["on_foot"])
        assert ctrl_default.variant == ctrl_one_mode.variant == "third_person"

    def test_at_threshold_routes_to_multi_mode(self):
        ctrl, _ = self._controller(locomotion="walk",
                                   locomotion_modes=["on_foot", "vehicle_car"])
        assert ctrl.variant == "multi_mode"
        assert ctrl.target == "player"
        assert [m["kind"] for m in ctrl.options["modes"]] == ["on_foot", "vehicle_car"]
        assert ctrl.options["default_mode"] == "on_foot"

    def test_flying_abilities_land_only_on_the_flying_entry(self):
        # src/unity_archetypes.py._score_flying_abilities is the producer
        # (gated on "flying" already clearing locomotion_modes); this is the
        # consumer side — flying_abilities must attach to the "flying" dict
        # specifically and leave every other kind's dict bare.
        ctrl, _ = self._controller(
            locomotion="fly",
            locomotion_modes=["on_foot", "vehicle_car", "flying"],
            flying_abilities=["flaps", "landing_gear"])
        by_kind = {m["kind"]: m for m in ctrl.options["modes"]}
        assert by_kind["flying"]["abilities"] == ["flaps", "landing_gear"]
        assert "abilities" not in by_kind["on_foot"]
        assert "abilities" not in by_kind["vehicle_car"]

    def test_no_flying_abilities_keeps_bare_kind_dicts(self):
        ctrl, _ = self._controller(locomotion="walk",
                                   locomotion_modes=["on_foot", "vehicle_car"])
        assert ctrl.options["modes"] == [{"kind": "on_foot"}, {"kind": "vehicle_car"}]

    def test_does_not_add_a_second_controller_subsystem(self):
        _, subs = self._controller(locomotion="walk",
                                   locomotion_modes=["on_foot", "vehicle_car"])
        assert sum(1 for s in subs if s.kind == "controller") == 1

    def test_input_variant_is_keyboard_mouse(self):
        _, subs = self._controller(locomotion="walk",
                                   locomotion_modes=["on_foot", "vehicle_car"])
        inp = next(s for s in subs if s.kind == "input")
        assert inp.variant == "keyboard_mouse"

    def test_physics_is_dynamic_when_default_mode_is_vehicle(self):
        _, subs = self._controller(locomotion="drive",
                                   locomotion_modes=["vehicle_car", "on_foot"])
        phys = next(s for s in subs if s.kind == "physics")
        assert phys.options.get("is_kinematic") is not True
        assert phys.options.get("mass") == 1200.0

    def test_physics_is_kinematic_when_default_mode_is_on_foot(self):
        _, subs = self._controller(locomotion="walk",
                                   locomotion_modes=["on_foot", "vehicle_car"])
        phys = next(s for s in subs if s.kind == "physics")
        assert phys.options.get("is_kinematic") is True

    def test_physics_mass_is_aircraft_when_flying_present_without_vehicle(self):
        _, subs = self._controller(locomotion="fly",
                                   locomotion_modes=["on_foot", "flying"])
        phys = next(s for s in subs if s.kind == "physics")
        assert phys.options.get("mass") == 2000.0

    def test_physics_mass_prefers_vehicle_car_over_flying(self):
        # Both share one Rigidbody — the car's tuned suspension mass wins,
        # matching flying_awake's own "only set mass when no vehicle_car"
        # deferral (see _controller_modes.j2).
        _, subs = self._controller(locomotion="drive",
                                   locomotion_modes=["vehicle_car", "flying"])
        phys = next(s for s in subs if s.kind == "physics")
        assert phys.options.get("mass") == 1200.0

    def test_physics_is_dynamic_when_default_mode_is_flying(self):
        _, subs = self._controller(locomotion="fly",
                                   locomotion_modes=["flying", "on_foot"])
        phys = next(s for s in subs if s.kind == "physics")
        assert phys.options.get("is_kinematic") is not True


# ── Schema-level validation (src/unity_schema.py) ────────────────────────────

class TestSchemaValidation:
    def test_empty_by_default(self):
        schema = _unity_schema()
        assert schema.locomotion_modes == []
        assert "locomotion_modes_lint" not in schema.meta

    def test_drops_unknown_kind_and_records_it(self):
        schema = _unity_schema(locomotion_modes=["on_foot", "swimming", "vehicle_car"])
        assert schema.locomotion_modes == ["on_foot", "vehicle_car"]
        assert schema.meta["locomotion_modes_lint"]["dropped_unknown_kind"] == ["swimming"]

    def test_flying_is_a_recognized_kind(self):
        schema = _unity_schema(locomotion_modes=["on_foot", "flying"])
        assert schema.locomotion_modes == ["on_foot", "flying"]
        assert "locomotion_modes_lint" not in schema.meta

    def test_dedupes_and_records_it(self):
        schema = _unity_schema(locomotion_modes=["on_foot", "on_foot", "vehicle_car"])
        assert schema.locomotion_modes == ["on_foot", "vehicle_car"]
        assert schema.meta["locomotion_modes_lint"]["dropped_duplicate_kind"] == ["on_foot"]


# ── Playability (src/unity_playability.py) ───────────────────────────────────

class TestPlayability:
    def test_ungraded_when_not_declared(self):
        rep = evaluate(_unity_schema())
        assert "multi_mode_registry" in rep.not_graded

    def test_errors_when_entries_were_dropped(self):
        rep = evaluate(_unity_schema(locomotion_modes=["on_foot", "swimming"],
                                     **_playable_avatar()))
        finding = next(f for f in rep.findings if f.check == "multi_mode_registry")
        assert finding.severity == ERROR

    def test_warns_on_a_single_surviving_entry(self):
        rep = evaluate(_unity_schema(locomotion_modes=["on_foot"], **_playable_avatar()))
        finding = next(f for f in rep.findings if f.check == "multi_mode_registry")
        assert finding.severity == WARN

    def test_passes_with_two_valid_modes(self):
        rep = evaluate(_unity_schema(locomotion_modes=["on_foot", "vehicle_car"],
                                     **_playable_avatar()))
        assert rep.checks["multi_mode_registry"] is True

    def test_avatar_and_controllable_checks_are_unaffected(self):
        # _check_avatar / _check_controllable read schema.entities/scripts,
        # never schema.locomotion_modes — a multi-mode project still needs
        # exactly the one Player-tagged entity every other project needs.
        rep = evaluate(_unity_schema(locomotion_modes=["on_foot", "vehicle_car"],
                                     **_playable_avatar()))
        assert rep.checks.get("avatar") is True
        assert rep.checks.get("controllable") is True

    def test_passes_with_on_foot_and_flying(self):
        rep = evaluate(_unity_schema(locomotion_modes=["on_foot", "flying"],
                                     **_playable_avatar()))
        assert rep.checks["multi_mode_registry"] is True
